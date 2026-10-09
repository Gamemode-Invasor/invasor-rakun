import { defineModule, ui } from "invasor";

type Def = Parameters<typeof defineModule>[0];
type Ctx = Parameters<NonNullable<Def["render"]>>[1];

const t = {
  up: "Rakun reachable at 127.0.0.1",
  down: "Rakun not reachable at 127.0.0.1",
  service: "Start Rakun",
  serviceHint: "On when Rakun is running",
  port: "Port",
  portLocked: "Stop Rakun to change the port",
  portHint: "Port Rakun will start on",
  open: "Open in the browser",
  hint: "Opens Rakun in Steam's browser",
  web: "Reachable from the network",
  webHint: "Off: only this device. On: any device on the local network",
  webLocked: "Stop Rakun to change it",
  openStopped: "Start Rakun to open its interface",
  author: "Author",
  program: "Program",
  installTitle: "Installation",
  notInstalled: "Rakun is not installed",
  installedLatest: (v: string) => `Rakun ${v} installed (latest version)`,
  installedOutdated: (v: string, l: string) => `Rakun ${v || "?"} installed, latest version: ${l}`,
  installedUnknown: "Rakun installed",
  download: "Download",
  downloadHint: "Downloads and installs the latest Rakun",
  update: "Update",
  updateHint: "Downloads and installs the latest Rakun",
  installing: "Installing…",
  installConfirm: "Download and install Rakun?",
  updateConfirm: "Update Rakun to the latest version?",
  installDone: "Rakun installed",
  ok: "OK",
  cancel: "Cancel",
  stopBusy: "Rakun has pending tasks (a download or a library refresh). Stop it anyway?",
  stopAnyway: "Stop anyway",
  keepRunning: "Keep running",
  logsTitle: "Logs",
  logsStopped: "Rakun is not running: start it in the Service tab to see its events",
  logsClear: "Clear",
  logsClearHint: "Empties the list",
  credits: "This interface belongs to Rakun, the service that installs Epic, GOG, Amazon and Zoom games into Steam.",
};

interface Stopped extends Status {
  busy?: boolean;
}

interface Install {
  installed: boolean;
  version: string;
  latest: string | null;
  update: boolean;
}

interface Status {
  active: boolean;
  reachable: boolean;
  port: number;
  web: "local" | "network";
}

interface Logs {
  id: number;
  lines: string[];
  running: boolean;
}

const LOG_LINES = 200;
const RAKUN_URL = "https://github.com/FranjeGueje/rakun";
const DEFAULT_PORT = 17999;
const MIN_PORT = 1024;
const MAX_PORT = 65535;
const REFRESH_MS = 3000;

function serviceTab(): NonNullable<Def["tabs"]>[number] {
  let timer: ReturnType<typeof setInterval> | undefined;
  let refresh: (() => Promise<void>) | undefined;

  return {
    label: "Service",
    async render(el, ctx) {
      const failed = (error: unknown) =>
        ctx.toast(error instanceof Error ? error.message : String(error), "error");
      let shown = "";
      let installing = false;
      // A poll that was already in flight when the user acted holds the old state: it must not be drawn.
      let acting = false;
      let generation = 0;

      // The page is drawn again only when something changed, so the slider is not reset every poll.
      const draw = async (force = false) => {
        if (installing || (acting && !force)) return;
        const started = generation;
        let s: Status;
        let i: Install;
        try {
          [s, i] = await Promise.all([ctx.call<Status>("service_status"), ctx.call<Install>("install_status")]);
        } catch (error) {
          failed(error);
          return;
        }
        if (started !== generation) return;
        const key = `${s.active}|${s.reachable}|${s.port}|${s.web}|${i.installed}|${i.version}|${i.latest}|${i.update}`;
        if (!force && key === shown) return;
        shown = key;

        const installInfo = !i.installed
          ? t.notInstalled
          : i.update
            ? t.installedOutdated(i.version, i.latest ?? "?")
            : i.version
              ? t.installedLatest(i.version)
              : t.installedUnknown;
        const installSection: HTMLElement[] = [ui.info(installInfo)];
        if (!i.installed || i.update) {
          const label = i.installed ? t.update : t.download;
          const button = ui.button({
            label,
            hint: i.installed ? t.updateHint : t.downloadHint,
            onClick: async () => {
              if (!(await ui.confirm(i.installed ? t.updateConfirm : t.installConfirm, { ok: t.ok, cancel: t.cancel }))) return;
              installing = true;
              button.setLabel(t.installing);
              button.setDisabled(true);
              try {
                await ctx.call("install");
                ctx.toast(t.installDone, "ok");
              } catch (error) {
                failed(error);
              }
              installing = false;
              await draw(true);
            },
          });
          installSection.push(button);
        }
        const off = i.installed ? undefined : t.notInstalled;

        while (el.firstChild) el.removeChild(el.firstChild);
        el.append(
          ui.section(t.installTitle, installSection, { open: false }),
          ui.info(`${s.reachable ? "🟢" : "🔴"} ${s.reachable ? t.up : t.down}:${s.port}`),
          ui.toggle({
            label: t.service,
            hint: off ?? t.serviceHint,
            value: s.active,
            disabled: !i.installed,
            onChange: async (on: boolean) => {
              acting = true;
              generation++;
              try {
                const done = await ctx.call<Stopped>("service_set", { on });
                if (done.busy && (await ui.confirm(t.stopBusy, { ok: t.stopAnyway, cancel: t.keepRunning }))) {
                  await ctx.call("service_set", { on, force: true });
                }
              } catch (error) {
                failed(error);
              }
              acting = false;
              await draw(true);
            },
          }),
          ui.number({
            label: t.port,
            hint: off ?? (s.active ? t.portLocked : t.portHint),
            min: MIN_PORT,
            max: MAX_PORT,
            step: 1,
            default: DEFAULT_PORT,
            value: s.port,
            disabled: s.active || !i.installed,
            onChange: async (port: number) => {
              try {
                await ctx.call("port_set", { port });
                shown = key.replace(`|${s.port}|`, `|${port}|`);
              } catch (error) {
                failed(error);
                await draw(true);
              }
            },
          }),
          ui.toggle({
            label: t.web,
            hint: off ?? (s.active ? t.webLocked : t.webHint),
            value: s.web === "network",
            disabled: s.active || !i.installed,
            onChange: async (on: boolean) => {
              const web = on ? "network" : "local";
              try {
                await ctx.call("web_set", { web });
                shown = key.replace(`|${s.web}|`, `|${web}|`);
              } catch (error) {
                failed(error);
                await draw(true);
              }
            },
          }),
          ui.button({
            label: t.open,
            hint: off ?? (s.active ? t.hint : t.openStopped),
            disabled: !i.installed || !s.active,
            onClick: async () => {
              try {
                await ctx.call("open_web");
              } catch (error) {
                failed(error);
              }
            },
          }),
        );
      };

      refresh = () => draw();
      await draw(true);
    },
    onShow() {
      clearInterval(timer);
      timer = setInterval(() => void refresh?.(), REFRESH_MS);
    },
    onHide() {
      clearInterval(timer);
      timer = undefined;
    },
  };
}

function logsTab(): NonNullable<Def["tabs"]>[number] {
  let timer: ReturnType<typeof setInterval> | undefined;
  let refresh: (() => Promise<void>) | undefined;

  return {
    label: t.logsTitle,
    async render(el, ctx) {
      const list = document.createElement("div");
      const state = ui.info(t.logsStopped);
      let since = 0;
      let running = false;
      let busy = false;

      const poll = async () => {
        if (busy) return;
        busy = true;
        try {
          const logs = await ctx.call<Logs>("logs_get", { since });
          running = logs.running;
          state.style.display = running || logs.lines.length ? "none" : "";
          for (const line of logs.lines) list.append(ui.info(line));
          while (list.childElementCount > LOG_LINES) list.firstElementChild?.remove();
          if (logs.lines.length) list.lastElementChild?.scrollIntoView({ block: "nearest" });
          since = logs.id;
        } catch (error) {
          ctx.toast(error instanceof Error ? error.message : String(error), "error");
        } finally {
          busy = false;
        }
      };

      el.append(
        state,
        list,
        ui.button({
          label: t.logsClear,
          hint: t.logsClearHint,
          onClick: async () => {
            try {
              await ctx.call("logs_clear");
            } catch (error) {
              ctx.toast(error instanceof Error ? error.message : String(error), "error");
            }
            while (list.firstChild) list.removeChild(list.firstChild);
            await poll();
          },
        }),
      );
      refresh = poll;
      await poll();
    },
    onShow() {
      clearInterval(timer);
      timer = setInterval(() => void refresh?.(), REFRESH_MS);
    },
    onHide() {
      clearInterval(timer);
      timer = undefined;
    },
  };
}

export default defineModule({
  tabsAlign: "justify",
  tabs: [
    serviceTab(),
    logsTab(),
    {
      label: "Credits",
      async render(el) {
        el.append(
          ui.info(t.credits),
          ui.info(`${t.program}: Rakun — ${RAKUN_URL}`),
          ui.info(`${t.author}: FranjeGueje`),
        );
      },
    },
  ],
});
