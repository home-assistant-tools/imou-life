# Standalone Imou bridge image (go2rtc + dh-p2p + discovery UI).
# Used by the Dockge compose stack; mirrors addon/imou-p2p-bridge.
FROM alpine:3.20

ARG DH_P2P_REF=9bb723ee5d6a08e6d51c0be8781bb6d6c2b5db4c
ARG GO2RTC_VERSION=1.9.14

RUN set -eux; \
    apk add --no-cache ca-certificates curl ffmpeg python3 py3-paho-mqtt; \
    apk add --no-cache --virtual .build build-base cargo git rust; \
    git clone --depth 1 https://github.com/khoanguyen-3fc/dh-p2p /tmp/dh-p2p; \
    cd /tmp/dh-p2p; \
    git fetch --depth 1 origin "${DH_P2P_REF}"; \
    git checkout "${DH_P2P_REF}"; \
    cargo build --release; \
    install -D -m 0755 target/release/dh-p2p /opt/dh-p2p/dh-p2p; \
    cd /; rm -rf /tmp/dh-p2p /root/.cargo; \
    apk del .build; \
    case "$(apk --print-arch)" in \
        aarch64) A=arm64 ;; x86_64) A=amd64 ;; *) echo bad arch; exit 1 ;; \
    esac; \
    curl -fsSL -o /opt/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${A}"; \
    chmod 0755 /opt/go2rtc

COPY supervisor.py login_site.py mqtt_bridge.py dvrip.py onvif_ptz_shim.py /opt/imou-p2p-bridge/
RUN chmod 0755 /opt/imou-p2p-bridge/*.py

ENV IMOU_BRIDGE_OPTIONS=/data/options.json
CMD ["python3", "/opt/imou-p2p-bridge/supervisor.py"]
