// A CSV file the browser saves (0.11.0: the Mobius values and the SNMP values). Excel-friendly: a BOM, CRLF,
// and text that starts like a formula (= + - @) gets a leading ' so a spreadsheet never runs it.

export function downloadCsv(name, header, rows) {
  const cell = (v) => {
    let s = typeof v === "number" ? String(v) : String(v ?? "");
    if (typeof v !== "number" && /^[=+\-@]/.test(s)) s = `'${s}`;
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const text = [header, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([`﻿${text}`], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
