# ROS 2 Jazzy with SafeGate installed, for the ros2 runner.
#   docker build -f docker/ros2/Dockerfile -t safegate-ros:jazzy .
FROM ros:jazzy-ros-base

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /opt/safegate/
COPY src /opt/safegate/src

# System site packages keep rclpy and the numpy it was built against.
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir /opt/safegate

ENV PATH=/opt/venv/bin:$PATH
ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
