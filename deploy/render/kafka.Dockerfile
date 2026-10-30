# Single-node KRaft Kafka for the Render demo (private service).
# Advertises its private-network IP: a Blueprint can't interpolate its own hostname, and the
# address must be reachable by clients. Topics auto-create on first use.
FROM apache/kafka:3.9.1
ENV KAFKA_NODE_ID=1 \
    KAFKA_PROCESS_ROLES=broker,controller \
    KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \
    KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
    KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
    KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
    KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
    KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
    KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
    KAFKA_NUM_PARTITIONS=3 \
    KAFKA_HEAP_OPTS="-Xms256m -Xmx512m"
CMD ["/bin/sh", "-c", "export KAFKA_ADVERTISED_LISTENERS=\"PLAINTEXT://$(hostname -i | cut -d' ' -f1):9092\"; exec /etc/kafka/docker/run"]
