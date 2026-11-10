// Mobius clients (0.9.6): every client PC Mobius reports - also the ones that are not desks in the
// inventory - with its values (the Mobius map's mobius_client values) and when it was last seen. On the
// Control Room tab and the Mobius Server's page. Shown as it is: the cards and alarms come from desks
// in devices.xlsx.

import { get } from "../api.js";
import { clear, h, setText } from "../dom.js";
import { fmtAgo, fmtValue, fmtWhen, metricSpec } from "../fmt.js";
import { dot } from "../status.js";
import { store } from "../store.js";

const EVERY_MS = 15000;

export function clientsPanel(nav) {
  if (store.meta.mode !== "live") return null;
  const hint = h("span", { class: "hint" });
  const body = h("div", {});
  const el = h("section", { class: "section hidden" }, h("h2", {}, "Mobius clients ", hint), body);
  let timer = null;

  const link = (text, href) => h("a", { href, text, onclick: (e) => { e.preventDefault(); nav(href); } });

  async function load() {
    let d;
    try { d = await get("/api/mobius/clients"); } catch { return; }       // restarting: keep what is shown
    if (!d.enabled) { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    const now = Date.now() / 1000;
    const on = d.clients.filter((c) => c.connected).length;
    setText(hint, `${on} connected`);
    clear(body);
    const conns = d.connections || [];
    if (!d.clients.length) body.append(h("p", { class: "muted", text: "Mobius has reported no client PC in the last 5 minutes." }));
    const cols = d.columns || [];
    if (d.clients.length) body.append(h("div", { class: "panel table-scroll" }, h("table", { class: "comps clients" },
      h("thead", {}, h("tr", {}, ...["PC", "Mobius", ...cols.map((k) => metricSpec(k).label)].map((t) => h("th", { text: t })))),
      h("tbody", {}, d.clients.map((c) => {
        const g = c.group && store.groups[c.group];
        return h("tr", {},
          h("td", {}, h("b", { text: c.name }), g ? h("div", { class: "muted small" }, "desk ", link(g.name, `#/${g.tab}/${g.id}`)) : null),
          h("td", {}, dot(c.connected ? 0 : 2), " ", c.connected ? `Connected · ${fmtAgo(now - c.last)}` : `Last seen ${fmtWhen(c.last)}`),
          ...cols.map((k) => h("td", { class: "num", text: fmtValue(k, c.values[k]) })));
      })))),
      h("p", { class: "muted small", text: "Every client PC Mobius reports, from the Mobius InfluxDB. To give a PC its own card "
        + "and alarms, add it as a desk in devices.xlsx (Groups: Kind client; Devices: Type mobius_client, with the PC's "
        + "name as shown here as its Name or Hostname), then rebuild the inventory." }));
    if (conns.length) {                                // the connections to the Mobius server, by address (0.9.7)
      const open = conns.filter((x) => x.connected).length;
      body.append(h("h3", { class: "mob-h3", text: `Client connections (${open} connected)` }),
        h("div", { class: "panel table-scroll" }, h("table", { class: "comps clients" },
          h("thead", {}, h("tr", {}, ...["Address", "Connection"].map((t) => h("th", { text: t })))),
          h("tbody", {}, conns.map((x) => h("tr", {}, h("td", { class: "num", text: x.address }),
            h("td", {}, dot(x.connected ? 0 : 2), " ", x.connected ? `Connected since ${fmtWhen(x.since)}`
              : `Disconnected ${fmtWhen(x.since)}`)))))),
        h("p", { class: "muted small", text: "Where Mobius clients connected from in the last day, from the Mobius InfluxDB. "
          + "Mobius does not say which PC an address is; an address can be a VPN or a router in front of several PCs." }));
    }
  }

  return {
    el,
    start() { load(); timer = setInterval(load, EVERY_MS); },
    destroy() { if (timer) clearInterval(timer); },
  };
}
