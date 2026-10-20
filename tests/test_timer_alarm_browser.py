"""Exercise the real Web Audio graph and dismissal without microphone/server models."""
from pathlib import Path
from playwright.sync_api import sync_playwright


def test_alarm_repeats_until_dismissed():
    source = (Path(__file__).parents[1] / "live/static/index.html").read_text(encoding="utf-8")
    alarm = source[source.index("let alarmNodes="):source.index("function cancelPlayback()")]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.set_content('<button id="start">Start</button><button id="alarmstop" hidden>Stop alarm</button>')
        page.evaluate("""code => {
            window.$ = id => document.getElementById(id);
            window.log = () => {};
            window.sent = [];
            window.ws = {readyState: 1, send: value => sent.push(JSON.parse(value))};
            window.ctx = null;
            // Treat any automatic timeout as a regression: expiry must be persistent.
            window.setTimeout = () => { throw new Error('Alarm must not auto-dismiss'); };
            const script = document.createElement("script"); script.textContent = code; document.body.appendChild(script);
            $('start').onclick = () => {
                ctx = new AudioContext();
                window.analyser = ctx.createAnalyser();
                analyser.connect(ctx.destination);
                const makeGain = ctx.createGain.bind(ctx);
                ctx.createGain = () => { const g = makeGain(); g.connect(analyser); return g; };
                startTimerAlarm();
            };
        }""", alarm)
        page.click("#start")
        page.wait_for_function("() => { const a=new Float32Array(analyser.fftSize); analyser.getFloatTimeDomainData(a); return a.some(x=>Math.abs(x)>0.01); }")
        page.wait_for_timeout(1700)
        assert page.is_visible("#alarmstop")
        assert page.evaluate("alarmInterval !== null")
        page.click("#alarmstop")
        assert page.evaluate("alarmInterval === null && alarmNodes.size === 0")
        assert page.evaluate("sent") == [{"t": "timer_alarm_stop"}]
        assert not errors
        browser.close()
