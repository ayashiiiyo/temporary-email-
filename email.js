#!/usr/bin/env node
const CF_TOKEN = process.env.CF_TOKEN || "YOUR_CF_API_TOKEN";
const CF_ACCOUNT = process.env.CF_ACCOUNT || "YOUR_CF_ACCOUNT_ID";
const CF_ZONE = process.env.CF_ZONE || "YOUR_CF_ZONE_ID";
const WORKER_NAME = process.env.WORKER_NAME || "tempmail-router";
const DOMAIN = process.env.DOMAIN || "yourdomain.com";

const API = "https://api.cloudflare.com/client/v4";
const H = { Authorization: `Bearer ${CF_TOKEN}`, "Content-Type": "application/json" };

function log(icon, msg) { console.log(`[${icon}] ${msg}`); }

async function cf(path, method = "GET", body = null) {
  const opts = { method, headers: H };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`${API}${path}`, opts);
  const data = await res.json().catch(() => ({}));
  return { status: res.status, data };
}

async function checkDNS() {
  log("i", "DNS MX records (via Cloudflare DOH)...");
  try {
    const res = await fetch(`https://cloudflare-dns.com/dns-query?name=${DOMAIN}&type=MX`, {
      headers: { accept: "application/dns-json" },
    });
    const d = await res.json();
    const mx = (d.Answer || []).filter((a) => a.type === 15);
    if (mx.length === 0) { log("x", "No MX records found!"); return false; }
    for (const r of mx) {
      const parts = r.data.split(" ");
      log(mx.some((m) => m.data.includes("cloudflare")) ? "v" : "x", `  MX ${parts[0]} ${parts[1]}`);
    }
    const hasCF = mx.some((m) => m.data.includes("route1.mx.cloudflare.net") || m.data.includes("route2") || m.data.includes("route3"));
    if (hasCF) { log("v", "MX -> Cloudflare Email Routing: OK"); return true; }
    log("x", "MX does NOT point to Cloudflare. Email Routing MX not set.");
    return false;
  } catch (e) { log("x", `DNS check failed: ${e.message}`); return false; }
}

async function checkEmailRouting() {
  log("i", "Email Routing status...");
  const { status, data } = await cf(`/zones/${CF_ZONE}/email/routing`);
  if (status === 403 || !data.success) {
    log("!", `Email Routing API returned ${status} (token lacks permission)`);
    return null;
  }
  const r = data.result || {};
  log(r.status === "ready" ? "v" : "x", `  status: ${r.status || "unknown"}`);
  log(r.enabled ? "v" : "x", `  enabled: ${r.enabled}`);
  return r;
}

async function checkCatchAll() {
  log("i", "Catch-all rule...");
  const { status, data } = await cf(`/zones/${CF_ZONE}/email/routing/rules/catch_all`);
  if (status === 403 || !data.success) {
    log("!", `Catch-all API returned ${status} (token lacks permission)`);
    return null;
  }
  const r = data.result || {};
  log(r.enabled ? "v" : "x", `  enabled: ${r.enabled}`);
  for (const a of r.actions || []) {
    log(a.type === "worker" ? "v" : "x", `  action: ${a.type} ${a.value ? JSON.stringify(a.value) : ""}`);
  }
  if (r.matchers) log("i", `  matchers: ${JSON.stringify(r.matchers)}`);
  return r;
}

async function checkWorker() {
  log("i", "Worker deployment...");
  const { status, data } = await cf(`/accounts/${CF_ACCOUNT}/workers/scripts`);
  if (data.success) {
    const found = (data.result || []).find((s) => s.id === WORKER_NAME);
    if (found) {
      log("v", `  worker "${found.id}" deployed`);
      const hasEmail = (found.handlers || []).includes("email");
      log(hasEmail ? "v" : "x", `  email handler: ${hasEmail ? "yes" : "no"}`);
      return true;
    }
    log("x", `  worker "${WORKER_NAME}" not found among ${data.result.length} scripts`);
    return false;
  }
  log("x", `Worker list failed (status ${status}): ${JSON.stringify(data.errors || data)}`);
  return false;
}

async function fixCatchAll() {
  log("i", "Attempting to set catch-all -> Worker...");
  const body = {
    name: "Catch-all to Worker",
    enabled: true,
    matchers: [{ type: "all" }],
    actions: [{ type: "worker", value: [WORKER_NAME] }],
  };
  const { status, data } = await cf(`/zones/${CF_ZONE}/email/routing/rules/catch_all`, "PUT", body);
  if (data.success) {
    log("v", "Catch-all set to Worker successfully!");
    return true;
  }
  log("x", `Failed to set catch-all: ${JSON.stringify(data.errors)}`);
  if (status === 403) {
    log("!", "Token lacks Email Routing edit permission.");
  }
  return false;
}

async function enableEmailRouting() {
  log("i", "Attempting to enable Email Routing...");
  const { status, data } = await cf(`/zones/${CF_ZONE}/email/routing/enable`, "POST");
  if (data.success) { log("v", "Email Routing enabled!"); return true; }
  log("x", `Enable failed: ${JSON.stringify(data.errors)}`);
  return false;
}

async function checkVPS() {
  log("i", "VPS backend health...");
  try {
    const res = await fetch("https://mail.yourdomain.com/healthz");
    if (res.ok) { log("v", "  VPS backend reachable (HTTP 200)"); return true; }
    log("x", `  VPS returned ${res.status}`); return false;
  } catch (e) { log("x", `  VPS unreachable: ${e.message}`); return false; }
}

async function main() {
  const mode = process.argv[2] || "check";
  console.log(`\n=== Temp Mail Diagnostic (${mode}) ===\n`);

  const mxOK = await checkDNS();
  console.log();
  const routingOK = await checkEmailRouting();
  console.log();
  const catchAllOK = await checkCatchAll();
  console.log();
  const workerOK = await checkWorker();
  console.log();
  const vpsOK = await checkVPS();
  console.log();

  if (mode === "fix") {
    console.log("\n--- FIX MODE ---\n");
    if (routingOK === null) { await enableEmailRouting(); console.log(); }
    if (catchAllOK === null || !catchAllOK.enabled || !(catchAllOK.actions || []).some((a) => a.type === "worker")) {
      await fixCatchAll();
      console.log();
    }
    console.log("--- Re-checking ---\n");
    await checkCatchAll();
  }

  console.log("\n=== Summary ===");
  log(mxOK ? "v" : "x", `MX records: ${mxOK ? "Cloudflare" : "NOT Cloudflare"}`);
  log(routingOK?.status === "ready" ? "v" : "!", `Email Routing: ${routingOK?.status || "unknown/inaccessible"}`);
  const caWorker = (catchAllOK?.actions || []).some((a) => a.type === "worker");
  log(caWorker ? "v" : "x", `Catch-all -> Worker: ${caWorker ? "yes" : "no/not set"}`);
  log(workerOK ? "v" : "x", `Worker deployed: ${workerOK ? "yes" : "no"}`);
  log(vpsOK ? "v" : "x", `VPS backend: ${vpsOK ? "online" : "offline"}`);

  const allOK = mxOK && caWorker && workerOK && vpsOK;
  console.log();
  log(allOK ? "v" : "x", allOK ? "ALL GOOD - email should work!" : "Issues found - see above");
  console.log();
}

main().catch((e) => { console.error(e); process.exit(1); });
