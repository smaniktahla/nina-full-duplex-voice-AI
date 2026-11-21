// Kiosk stats: "CPU 35%  RAM 2.1/11G  Disk 23%" in the GNOME top bar.
// Reads /proc/stat, /proc/meminfo and statfs in-process (no subprocess), refreshed every INTERVAL_S.
// Turns amber at WARN percent and red at CRIT percent on any of the three.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

const INTERVAL_S = 3;
const WARN = 85;
const CRIT = 95;
const DISK_PATH = '/';

const decoder = new TextDecoder();

function readText(path) {
    const [ok, bytes] = Gio.File.new_for_path(path).load_contents(null);
    return ok ? decoder.decode(bytes) : '';
}

const Indicator = GObject.registerClass(
class KioskStatsIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Kiosk stats', true);
        this._label = new St.Label({text: 'CPU --', y_align: Clutter.ActorAlign.CENTER});
        this.add_child(this._label);
        this._prev = null;
        this._update();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, INTERVAL_S, () => {
            this._update();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _cpu() {
        const f = readText('/proc/stat').split('\n')[0].trim().split(/\s+/).slice(1).map(Number);
        const idle = f[3] + (f[4] || 0);
        const total = f.reduce((a, b) => a + b, 0);
        let pct = 0;
        if (this._prev && total > this._prev.total)
            pct = 100 * (1 - (idle - this._prev.idle) / (total - this._prev.total));
        this._prev = {idle, total};
        return Math.max(0, Math.min(100, Math.round(pct)));
    }

    _mem() {
        const m = {};
        for (const line of readText('/proc/meminfo').split('\n')) {
            const [k, v] = line.split(':');
            if (v)
                m[k] = parseInt(v, 10);   // kB
        }
        const used = m.MemTotal - m.MemAvailable;
        return {used: used / 1048576, total: m.MemTotal / 1048576, pct: Math.round(100 * used / m.MemTotal)};
    }

    _disk() {
        const info = Gio.File.new_for_path(DISK_PATH).query_filesystem_info('filesystem::size,filesystem::free', null);
        const size = info.get_attribute_uint64('filesystem::size');
        const free = info.get_attribute_uint64('filesystem::free');
        return size ? Math.round(100 * (size - free) / size) : 0;
    }

    _update() {
        try {
            const cpu = this._cpu();
            const mem = this._mem();
            const disk = this._disk();
            this._label.set_text(`CPU ${cpu}%  RAM ${mem.used.toFixed(1)}/${Math.round(mem.total)}G  Disk ${disk}%`);
            const worst = Math.max(cpu, mem.pct, disk);
            this._label.set_style(worst >= CRIT ? 'color: #ff5555;' : worst >= WARN ? 'color: #ffb454;' : null);
        } catch (e) {
            this._label.set_text('stats n/a');
        }
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = null;
        }
        super.destroy();
    }
});

export default class KioskStatsExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
