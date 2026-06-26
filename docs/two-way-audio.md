# Two-Way Audio

## Main Finding

Two-way audio is handled by a dedicated native talk component. It is not simply
the audio track from the live RTSP stream.

Java wrapper:

```text
com/iotcom/commonsdk/talk/NativeAudioTalker.java
```

Higher-level wrappers:

```text
com/lc/common/talk/AudioTalker.java
com/lc/lcsdk/LCSDK_Talk.java
com/lc/lcsdk/utils/TalkUtils.java
```

Native library:

```text
libCommonSDK.so
```

## Native Methods

`NativeAudioTalker` exposes:

```text
createAudioTalker(String json)
destroyAudioTalker(long handle)
startTalk(long handle)
stopTalk(long handle)
startSampleAudio(long handle)
stopSampleAudio(long handle)
playSound(long handle)
stopSound(long handle)
pushMediaData(long handle, int type, byte[] data, int length, boolean softEncode)
enableTalkVideo(long handle, boolean enabled)
getStreamMode(long handle)
isShareLink(long handle)
isOptHandleOK(long handle, String key)
setAecEnable(long handle, boolean enabled)
setHardwareAecEnable(long handle, boolean enabled)
setAudioRecScaling(long handle, float ratio)
setSpeechChange(long handle, boolean enabled, int effect, float tsm)
setAceDebugSavePath(long handle, String path)
setListener(long handle, Object listener)
```

## Talk Transport Clues

Native strings indicate both RTSP talk and HTTP/XAV/DHHTTP talk paths:

```text
enter RTSPTalker::setupStream
RTSPTalker::getStream
enter DeviceTalker::getStream
DHHTTPTalker::getStream
subtype=talkback
visualtalk_reqid
/live/visualtalk.xav
SharedTalk
Local.StreamRtsp
onTalkBackDadaProc
```

There are also relay and P2P references in the same native talk flow:

```text
P2PChannelRequestTimeout
onP2PConnectSuccess
Relay
RELAY
connect Tcp Relay Agent
```

## Implication for Home Assistant

For read-only camera use, a bridge only needs to expose local RTSP or HTTP media
to go2rtc/Frigate.

For talkback, the bridge needs a separate control path:

```text
Home Assistant or go2rtc microphone audio
  -> bridge process
  -> NativeAudioTalker.pushMediaData(...)
  -> native P2P/relay tunnel
  -> camera speaker
```

This is a larger task than exposing the video stream because it requires codec,
sample format, talk session setup, echo cancellation choices, and correct
start/stop lifecycle handling.

