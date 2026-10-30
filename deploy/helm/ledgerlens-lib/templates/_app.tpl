{{/*
One template set for every LedgerLens service. A service chart is values.yaml plus
`{{ include "ledgerlens.app" . }}` (or "ledgerlens.cronjob"). Values contract:
  image.{repository,tag,pullPolicy}, replicas, port (null = worker: no Service, no HTTP probes),
  command (optional override), config (-> ConfigMap, envFrom),
  secretEnv (ENV_NAME: key in the shared Secret global.secretName), optionalSecretEnv (names allowed to be absent),
  resources, hpa.{enabled,minReplicas,maxReplicas,cpuUtilization}, probes.{liveness,readiness},
  service.{type,nodePort}
*/}}

{{- define "ledgerlens.name" -}}{{ .Chart.Name }}{{- end -}}

{{- define "ledgerlens.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: ledgerlens
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "ledgerlens.selector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* No runAsUser anywhere: OpenShift's restricted-v2 SCC injects an arbitrary UID (in group 0). */}}
{{- define "ledgerlens.podSecurity" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "ledgerlens.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "ledgerlens.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "ledgerlens.name" . }}
{{- if .Values.secretEnv }}
env:
  {{- range $name, $key := .Values.secretEnv }}
  - name: {{ $name }}
    valueFrom:
      secretKeyRef:
        name: {{ $.Values.global.secretName }}
        key: {{ $key }}
        {{- if has $name ($.Values.optionalSecretEnv | default list) }}
        optional: true
        {{- end }}
  {{- end }}
{{- end }}
{{- end -}}

{{/* Read-only root FS: only `writablePaths` (default /tmp) get an emptyDir each. */}}
{{- define "ledgerlens.writableMounts" -}}
volumeMounts:
  {{- range $i, $p := .Values.writablePaths }}
  - name: writable-{{ $i }}
    mountPath: {{ $p }}
  {{- end }}
{{- end -}}

{{- define "ledgerlens.writableVolumes" -}}
volumes:
  {{- range $i, $p := .Values.writablePaths }}
  - name: writable-{{ $i }}
    emptyDir: {}
  {{- end }}
{{- end -}}

{{/*
Block until the Kafka topics this service uses exist (they're created by the infra chart's Job).
Without this, a consumer that starts first sees no partitions until its next metadata refresh (~5 min).
Uses the Kafka image's own CLI; LOG_DIR=/tmp because the root FS is read-only.
*/}}
{{- define "ledgerlens.waitForTopics" -}}
{{- if .Values.waitForTopics }}
initContainers:
  - name: wait-for-topics
    image: {{ .Values.global.kafkaToolsImage }}
    imagePullPolicy: IfNotPresent
    env:
      - { name: LOG_DIR, value: /tmp }
    command:
      - sh
      - -c
      - |
        for t in {{ join " " .Values.waitForTopics }}; do
          until /opt/kafka/bin/kafka-topics.sh --bootstrap-server {{ .Values.global.kafkaBootstrap }} --describe --topic "$t" > /dev/null 2>&1; do
            echo "waiting for topic $t"; sleep 3
          done
        done
    resources:
      requests: { cpu: 50m, memory: 128Mi }
      limits: { cpu: 500m, memory: 384Mi }
    securityContext:
      {{- include "ledgerlens.containerSecurity" . | nindent 6 }}
      {{- with .Values.global.kafkaToolsRunAsUser }}
      # The Kafka image's USER is a name (appuser), which runAsNonRoot can't verify, so pin its
      # numeric UID. Left unset on OpenShift, where the SCC assigns the UID instead.
      runAsUser: {{ . }}
      {{- end }}
    {{- include "ledgerlens.writableMounts" . | nindent 4 }}
{{- end }}
{{- end -}}

{{- define "ledgerlens.configmap" -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "ledgerlens.name" . }}
  labels: {{- include "ledgerlens.labels" . | nindent 4 }}
data:
  {{- range $k, $v := .Values.config }}
  {{ $k }}: {{ $v | toString | quote }}
  {{- end }}
{{- end -}}

{{- define "ledgerlens.app" -}}
{{ include "ledgerlens.configmap" . }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "ledgerlens.name" . }}
  labels: {{- include "ledgerlens.labels" . | nindent 4 }}
spec:
  {{- if not .Values.hpa.enabled }}
  replicas: {{ .Values.replicas }}
  {{- end }}
  selector:
    matchLabels: {{- include "ledgerlens.selector" . | nindent 6 }}
  template:
    metadata:
      labels: {{- include "ledgerlens.selector" . | nindent 8 }}
      annotations:
        # ConfigMaps aren't hot-reloaded; a config change must roll the pods.
        checksum/config: {{ include "ledgerlens.configmap" . | sha256sum }}
    spec:
      securityContext: {{- include "ledgerlens.podSecurity" . | nindent 8 }}
      terminationGracePeriodSeconds: 30
      {{- include "ledgerlens.waitForTopics" . | nindent 6 }}
      containers:
        - name: {{ include "ledgerlens.name" . }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          {{- with .Values.command }}
          command: {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- include "ledgerlens.env" . | nindent 10 }}
          {{- if .Values.port }}
          ports:
            - name: http
              containerPort: {{ .Values.port }}
          livenessProbe:
            httpGet: { path: {{ .Values.probes.liveness }}, port: http }
            initialDelaySeconds: 10
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet: { path: {{ .Values.probes.readiness }}, port: http }
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 3
          {{- end }}
          resources: {{- toYaml .Values.resources | nindent 12 }}
          securityContext: {{- include "ledgerlens.containerSecurity" . | nindent 12 }}
          {{- include "ledgerlens.writableMounts" . | nindent 10 }}
      {{- include "ledgerlens.writableVolumes" . | nindent 6 }}
{{- if .Values.port }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "ledgerlens.name" . }}
  labels: {{- include "ledgerlens.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  selector: {{- include "ledgerlens.selector" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .Values.port }}
      targetPort: http
      {{- if and (eq .Values.service.type "NodePort") .Values.service.nodePort }}
      nodePort: {{ .Values.service.nodePort }}
      {{- end }}
{{- end }}
{{- if .Values.hpa.enabled }}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "ledgerlens.name" . }}
  labels: {{- include "ledgerlens.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "ledgerlens.name" . }}
  minReplicas: {{ .Values.hpa.minReplicas }}
  maxReplicas: {{ .Values.hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.hpa.cpuUtilization }}
{{- end }}
{{- end -}}

{{- define "ledgerlens.cronjob" -}}
{{ include "ledgerlens.configmap" . }}
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "ledgerlens.name" . }}
  labels: {{- include "ledgerlens.labels" . | nindent 4 }}
spec:
  schedule: {{ .Values.schedule | quote }}
  concurrencyPolicy: Forbid # never run two nightly batches at once
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        metadata:
          labels: {{- include "ledgerlens.selector" . | nindent 12 }}
        spec:
          restartPolicy: Never
          securityContext: {{- include "ledgerlens.podSecurity" . | nindent 12 }}
          containers:
            - name: {{ include "ledgerlens.name" . }}
              image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
              imagePullPolicy: {{ .Values.image.pullPolicy }}
              {{- with .Values.command }}
              command: {{- toYaml . | nindent 16 }}
              {{- end }}
              {{- include "ledgerlens.env" . | nindent 14 }}
              resources: {{- toYaml .Values.resources | nindent 16 }}
              securityContext: {{- include "ledgerlens.containerSecurity" . | nindent 16 }}
              {{- include "ledgerlens.writableMounts" . | nindent 14 }}
          {{- include "ledgerlens.writableVolumes" . | nindent 10 }}
{{- end -}}
