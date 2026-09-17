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

# opendronemap/odm's base image sets ENTRYPOINT to its run.py wrapper, which
# expects CLI-style ODM args. RunPod v2's pod "args" field has no way to
# override ENTRYPOINT (unlike v1's dockerEntrypoint) - it just appends
# tokens to whatever ENTRYPOINT already is. Clearing it here means our
# "/bin/bash -c <boot script>" args become the whole command instead of
# being fed to run.py as bogus CLI flags.
ENTRYPOINT []