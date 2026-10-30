# syntax=docker/dockerfile:1
# Official Apache Spark image (Spark 3.5.7, Scala 2.12, Java 17, Python 3.10). Runs as uid 185 (non-root).
#   docker build -t ledgerlens/spark-aggregates jobs/spark_aggregates
FROM apache/spark:3.5.7-scala2.12-java17-python3-ubuntu AS runtime

# MongoDB Spark Connector 10.5.0 for Scala 2.12 + the Java driver range it declares ([5.1.1,5.1.99)).
# Baked into the image so the nightly job never resolves packages from the internet at run time.
ARG MAVEN=https://repo1.maven.org/maven2/org/mongodb
ADD --chmod=644 ${MAVEN}/spark/mongo-spark-connector_2.12/10.5.0/mongo-spark-connector_2.12-10.5.0.jar /opt/spark/jars/
ADD --chmod=644 ${MAVEN}/mongodb-driver-sync/5.1.4/mongodb-driver-sync-5.1.4.jar /opt/spark/jars/
ADD --chmod=644 ${MAVEN}/mongodb-driver-core/5.1.4/mongodb-driver-core-5.1.4.jar /opt/spark/jars/
ADD --chmod=644 ${MAVEN}/bson/5.1.4/bson-5.1.4.jar /opt/spark/jars/
ADD --chmod=644 ${MAVEN}/bson-record-codec/5.1.4/bson-record-codec-5.1.4.jar /opt/spark/jars/

# Lets plain `python3 -m pytest` import pyspark (spark-submit sets this up itself).
ENV PYTHONPATH=/opt/spark/python/lib/pyspark.zip:/opt/spark/python/lib/py4j-0.10.9.7-src.zip
WORKDIR /opt/job
COPY aggregates.py .
USER 185
CMD ["/opt/spark/bin/spark-submit", "--master", "local[2]", "--driver-memory", "1g", "/opt/job/aggregates.py"]

# --- test image: same runtime + pinned pytest (make test-spark) ---
FROM runtime AS test
USER root
COPY requirements-test.txt /tmp/
RUN pip3 install --no-cache-dir --require-hashes -r /tmp/requirements-test.txt
COPY tests tests
USER 185
CMD ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
