import json, sys, time, urllib.request
TOKEN = __import__("os").environ["HA_TOKEN"]  # long-lived token; see the homelab manifest
def say(text, agent="conversation.needle_voice", conv=None, timeout=240):
    body = {"text": text, "language": "en", "agent_id": agent}
    if conv: body["conversation_id"] = conv
    req = urllib.request.Request("http://homeassistant.local:8123/api/conversation/process", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, method="POST")
    t = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        dt = time.time() - t
        speech = r.get("response", {}).get("speech", {}).get("plain", {}).get("speech")
        return dt, r.get("response", {}).get("response_type"), speech, r.get("conversation_id"), r
    except Exception as e:
        return time.time() - t, "EXC", repr(e), None, None
if __name__ == "__main__":
    for text in sys.argv[1:]:
        dt, rtype, speech, cid, raw = say(text)
        print(f"{dt:6.2f}s [{rtype}] {text!r}\n        -> {speech!r}  (conversation_id={cid})", flush=True)
