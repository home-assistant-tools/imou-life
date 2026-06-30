# Standalone Imou bridge image (go2rtc + pure-Python DHP2P + discovery UI).
# Used by the Dockge compose stack; mirrors addon/imou-p2p-bridge.
FROM alpine:3.20

ARG GO2RTC_VERSION=1.9.14

RUN set -eux; \
    apk add --no-cache ca-certificates curl ffmpeg python3 py3-flask; \
    case "$(apk --print-arch)" in \
        aarch64) A=arm64 ;; x86_64) A=amd64 ;; *) echo bad arch; exit 1 ;; \
    esac; \
    curl -fsSL -o /opt/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${A}"; \
    chmod 0755 /opt/go2rtc

COPY supervisor.py app_ui.py login_site.py dvrip.py onvif_ptz_shim.py dlna_media_renderer.py imou_dhp2p.py imou_wsse.py imou_visualtalk.py imou_dhav.py imou_pure_talk.py frigate_imou_talk_exec.py /opt/imou-p2p-bridge/
COPY geelab_assets/ /opt/imou-p2p-bridge/geelab_assets/
RUN chmod 0755 /opt/imou-p2p-bridge/*.py

ENV IMOU_BRIDGE_OPTIONS=/data/options.json
CMD ["python3", "/opt/imou-p2p-bridge/supervisor.py"]
