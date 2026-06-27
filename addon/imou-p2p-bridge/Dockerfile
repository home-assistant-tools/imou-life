ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG DH_P2P_REF=9bb723ee5d6a08e6d51c0be8781bb6d6c2b5db4c
ARG GO2RTC_VERSION=1.9.14

# Build dh-p2p (Rust) and install go2rtc (static Go binary, fans out the
# single-client P2P tunnel to many consumers + adds WebRTC/HLS).
RUN set -eux; \
    apk add --no-cache \
        build-base \
        ca-certificates \
        cargo \
        curl \
        ffmpeg \
        git \
        py3-paho-mqtt \
        python3 \
        rust; \
    git clone --depth 1 https://github.com/khoanguyen-3fc/dh-p2p /tmp/dh-p2p; \
    cd /tmp/dh-p2p; \
    git fetch --depth 1 origin "${DH_P2P_REF}"; \
    git checkout "${DH_P2P_REF}"; \
    cargo build --release; \
    install -D -m 0755 target/release/dh-p2p /opt/dh-p2p/dh-p2p; \
    rm -rf /tmp/dh-p2p /root/.cargo; \
    case "$(apk --print-arch)" in \
        aarch64) GO2RTC_ARCH=arm64 ;; \
        x86_64)  GO2RTC_ARCH=amd64 ;; \
        *) echo "unsupported arch"; exit 1 ;; \
    esac; \
    curl -fsSL -o /opt/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GO2RTC_ARCH}"; \
    chmod 0755 /opt/go2rtc

COPY rootfs /

RUN chmod 0755 \
    /etc/services.d/imou-p2p-bridge/run \
    /opt/imou-p2p-bridge/supervisor.py \
    /opt/imou-p2p-bridge/login_site.py
