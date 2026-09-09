FROM opendronemap/odm:latest

# Suppress interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Force the container to use the primary US mirror instead of a broken regional mirror
RUN sed -i 's/archive.ubuntu.com/us.archive.ubuntu.com/g' /etc/apt/sources.list.d/ubuntu.sources || true && \
    sed -i 's/archive.ubuntu.com/us.archive.ubuntu.com/g' /etc/apt/sources.list || true

# Update and install dependencies
RUN apt-get -o Acquire::http::No-Cache=True \
            -o Acquire::http::Pipeline-Depth=0 \
            -o Acquire::BrokenProxy=true \
            update && \
    apt-get install -y openssh-server rsync curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set up SSH directories and generate host keys
RUN mkdir -p /root/.ssh /run/sshd && \
    chmod 700 /root/.ssh && \
    ssh-keygen -A