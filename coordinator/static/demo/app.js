/**
 * Demo page for M1. All encryption happens in ../dstore/client.js; this file
 * only wires the UI to it. Kept out of the HTML so the page can run under a
 * Content-Security-Policy that forbids inline scripts.
 */
import { DStoreClient } from "../dstore/client.js";

const client = new DStoreClient();
const $ = (id) => document.getElementById(id);
const resumable = new Map(); // fileId -> File, for uploads interrupted in this tab
let activeUploads = 0;

const fmtSize = (n) => {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[u]}`;
};
const fmtDate = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const show = (el, on = true) => { el.hidden = !on; };
const friendly = (err) => {
  if (err?.name === "IntegrityError") return `Integrity check failed: ${err.message}`;
  if (err?.status === 401) return err.body?.detail || "Not signed in.";
  if (err?.status === 429) return "Too many attempts. Wait a minute and try again.";
  if (err?.body && typeof err.body === "object") return Object.values(err.body).flat().join(" ");
  return err?.message || String(err);
};

// --- Tabs ---------------------------------------------------------------
document.querySelectorAll("[data-tab]").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    document.querySelectorAll("[data-panel]").forEach((p) => show(p, p.dataset.panel === tab.dataset.tab));
    show($("auth-error"), false);
  });
});

async function busy(form, label, task) {
  const button = form.querySelector("button[type=submit]");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  show($("auth-error"), false);
  try {
    await task();
  } catch (err) {
    $("auth-error").textContent = friendly(err);
    show($("auth-error"));
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function enterApp() {
  $("who-name").textContent = client.username;
  show($("who"));
  show($("auth-card"), false);
  show($("files-card"));
  document.querySelectorAll("input[type=password], textarea").forEach((i) => (i.value = ""));
  refresh();
}

$("signin-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.target, "Unlocking…", async () => {
    await client.login($("si-user").value, $("si-pass").value);
    enterApp();
  });
});

$("register-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.target, "Creating keys…", async () => {
    if ($("re-pass").value !== $("re-pass2").value) throw new Error("The passphrases don't match.");
    const { recoveryKey } = await client.register($("re-user").value, $("re-pass").value);
    $("recovery-key").textContent = recoveryKey;
    show($("auth-card"), false);
    show($("recovery-card"));
  });
});

$("recover-form").addEventListener("submit", (e) => {
  e.preventDefault();
  busy(e.target, "Re-wrapping keys…", async () => {
    await client.recover($("rc-user").value, $("rc-key").value, $("rc-pass").value);
    enterApp();
  });
});

$("copy-recovery").addEventListener("click", async () => {
  await navigator.clipboard?.writeText($("recovery-key").textContent);
  $("copy-recovery").textContent = "Copied";
});
$("ack-recovery").addEventListener("click", () => {
  $("recovery-key").textContent = "";
  show($("recovery-card"), false);
  enterApp();
});

$("sign-out").addEventListener("click", async () => {
  await client.logout().catch(() => {});
  location.reload();
});

// --- Files --------------------------------------------------------------
async function refresh() {
  try {
    const files = await client.listFiles();
    const list = $("files");
    list.replaceChildren(...files.map(renderFile));
    show($("empty"), files.length === 0);
  } catch (err) {
    showFilesError(err);
  }
}

function renderFile(file) {
  const li = document.createElement("li");
  const meta = document.createElement("div");
  meta.className = "meta";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = file.name ?? "(name could not be decrypted)";
  if (file.status !== "complete") {
    const pill = document.createElement("span");
    pill.className = "pill";
    pill.textContent = "incomplete";
    title.append(pill);
  }
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = `${fmtSize(file.size)} · ${fmtDate(file.createdAt)}`;
  meta.append(title, sub);
  li.append(meta);

  if (file.status === "complete") {
    li.append(action("Download", () => download(file)));
  } else if (resumable.has(file.id)) {
    li.append(action("Resume", () => upload(resumable.get(file.id), file.id)));
  }
  li.append(action("Delete", () => remove(file)));
  return li;
}

function action(label, handler) {
  const button = document.createElement("button");
  button.className = "secondary";
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await handler(); } finally { button.disabled = false; }
  });
  return button;
}

function showFilesError(err) {
  $("files-error").textContent = friendly(err);
  show($("files-error"));
}

function progressRow(name) {
  const row = document.createElement("div");
  row.className = "upload";
  row.innerHTML = '<div class="row"><span class="name"></span><span class="pct">0%</span></div><div class="bar"><span></span></div>';
  row.querySelector(".name").textContent = name;
  $("uploads").append(row);
  return {
    update(done, total) {
      const pct = total ? Math.floor((done / total) * 100) : 100;
      row.querySelector(".pct").textContent = `${pct}%`;
      row.querySelector(".bar span").style.width = `${pct}%`;
    },
    fail(message) { row.querySelector(".pct").textContent = message; },
    remove() { row.remove(); },
  };
}

async function upload(file, resumeId) {
  show($("files-error"), false);
  const row = progressRow(file.name);
  activeUploads++;
  let createdId = resumeId;
  try {
    const onProgress = ({ sentBytes, totalBytes }) => row.update(sentBytes, totalBytes);
    if (resumeId) {
      await client.resumeUpload(resumeId, file, { onProgress });
    } else {
      await client.uploadFile(file, { onProgress, onCreated: (id) => (createdId = id) });
    }
    resumable.delete(createdId);
    row.remove();
  } catch (err) {
    if (createdId) resumable.set(createdId, file);
    row.fail("failed");
    showFilesError(err);
    setTimeout(() => row.remove(), 4000);
  } finally {
    activeUploads--;
    refresh();
  }
}

async function download(file) {
  show($("files-error"), false);
  const row = progressRow(`↓ ${file.name}`);
  try {
    const result = await client.downloadFile(file.id, {
      onProgress: ({ receivedBytes, totalBytes }) => row.update(receivedBytes, totalBytes),
    });
    const url = URL.createObjectURL(result.blob);
    const link = Object.assign(document.createElement("a"), { href: url, download: result.name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (err) {
    showFilesError(err);
  } finally {
    row.remove();
  }
}

async function remove(file) {
  if (!confirm(`Delete "${file.name ?? "this file"}"? This can't be undone.`)) return;
  try {
    await client.deleteFile(file.id);
    resumable.delete(file.id);
  } catch (err) {
    showFilesError(err);
  }
  refresh();
}

const drop = $("drop");
$("file-input").addEventListener("change", (e) => {
  [...e.target.files].forEach((f) => upload(f));
  e.target.value = "";
});
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  [...e.dataTransfer.files].forEach((f) => upload(f));
});

addEventListener("beforeunload", (e) => {
  if (activeUploads > 0) e.preventDefault();
});
