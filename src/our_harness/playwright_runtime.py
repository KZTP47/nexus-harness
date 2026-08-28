from __future__ import annotations

import json
import ipaddress
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit


@dataclass(frozen=True)
class BundledPlaywrightRuntime:
    root: Path
    node: Path
    cli: Path
    playwright_module: Path
    test_module: Path
    browsers: Path
    chromium: Path
    node_version: str
    playwright_version: str
    chromium_revision: str

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        return {
            **dict(base or {}),
            "PLAYWRIGHT_BROWSERS_PATH": str(self.browsers),
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
            "NEXUS_BUNDLED_PLAYWRIGHT_ROOT": str(self.root),
        }


PRIVATE_NETWORK_CLIENT_SERVER = "S-1-15-3-3"
INTERNET_CLIENT = "S-1-15-3-1"


def normalize_approved_https_base_url(value: str) -> tuple[str, str, str, int]:
    """Return canonical base URL, origin, ASCII host and port; reject substitutes."""

    raw = str(value).strip()
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("Playwright baseURL must be an exact approved https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Approved Playwright baseURL may not contain credentials, query, or fragment")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").casefold()
        port = int(parsed.port or 443)
    except (UnicodeError, ValueError) as error:
        raise ValueError("Approved Playwright baseURL has an invalid authority") from error
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("An approved https origin may not be substituted by localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        raise ValueError("An approved https origin may not be substituted by loopback")
    authority_host = f"[{host}]" if address is not None and address.version == 6 else host
    authority = authority_host if port == 443 else f"{authority_host}:{port}"
    origin = f"https://{authority}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise ValueError("Approved Playwright baseURL path must be absolute")
    base_url = urlunsplit(("https", authority, path, "", ""))
    return base_url, origin, host, port


_EXACT_ORIGIN_PROXY_SOURCE = r'''
const net = require('node:net');
const host = String(process.argv[1]).toLowerCase();
const port = Number(process.argv[2]);
const listenHost = process.argv[3];
const listenPort = Number(process.argv[4]);
const stopToken = process.argv[5];
const configuredOrigin = process.argv[6];
const sockets = new Set();
function record(value) { console.log('NEXUS_ORIGIN_ROUTE ' + JSON.stringify({...value, configured_origin: configuredOrigin})); }
const server = net.createServer(client => {
  sockets.add(client);
  client.on('close', () => sockets.delete(client));
  client.on('error', () => {});
  client.once('data', first => {
    const line = first.toString('latin1').split(/\r?\n/, 1)[0] || '';
    const control = /^NEXUS_STOP\s+([^\s]+)$/i.exec(line);
    if (control) {
      const allowed = control[1] === stopToken;
      record({route: 'proxy-control', authority: '', allowed, tls_tunnel: false});
      if (!allowed) { client.end('DENIED\r\n'); return; }
      client.end('OK\r\n');
      for (const active of sockets) if (active !== client) active.destroy();
      server.close(() => process.exit(0));
      setTimeout(() => process.exit(0), 1000).unref();
      return;
    }
    const match = /^CONNECT\s+([^\s]+)\s+HTTP\/1\.[01]$/i.exec(line);
    const authority = match ? match[1] : '';
    const split = authority.lastIndexOf(':');
    const requestedHost = (split > 0 ? authority.slice(0, split) : authority).replace(/^\[|\]$/g, '').toLowerCase();
    const requestedPort = Number(split > 0 ? authority.slice(split + 1) : 443);
    const allowed = Boolean(match && requestedHost === host && requestedPort === port);
    record({route: 'https-connect', authority, allowed, tls_tunnel: allowed});
    if (!allowed) { client.end('HTTP/1.1 403 Exact Origin Required\r\nConnection: close\r\n\r\n'); return; }
    const upstream = net.connect({host, port}, () => {
      client.write('HTTP/1.1 200 Connection Established\r\nProxy-Agent: Nexus-Exact-Origin\r\n\r\n');
      const rest = first.subarray(first.indexOf('\r\n\r\n') + 4);
      if (rest.length) upstream.write(rest);
      client.pipe(upstream); upstream.pipe(client);
    });
    sockets.add(upstream); upstream.on('close', () => sockets.delete(upstream));
    upstream.on('error', error => { record({route: 'upstream-error', authority, allowed: true, error: error.code || error.message}); client.destroy(); });
    client.on('error', () => upstream.destroy());
  });
});
server.on('error', error => { console.error(error); process.exit(1); });
server.listen(listenPort, listenHost, () => console.log('NEXUS_EXACT_ORIGIN_PROXY_READY ' + listenHost + ':' + listenPort));
'''


_EXACT_ORIGIN_PROXY_CLOSE_SOURCE = r'''
const net = require('node:net');
const host = process.argv[1];
const port = Number(process.argv[2]);
const token = process.argv[3];
const socket = net.connect({host, port}, () => socket.write('NEXUS_STOP ' + token + '\r\n'));
let response = '';
socket.on('data', chunk => { response += chunk.toString('ascii'); });
socket.on('end', () => process.exit(response.startsWith('OK') ? 0 : 1));
socket.on('error', error => { console.error(error); process.exit(1); });
setTimeout(() => { console.error('proxy control timeout'); process.exit(1); }, 5000).unref();
'''


_INPROCESS_PLAYWRIGHT_WORKER_SHIM_SOURCE = r'''
const cp = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const Module = require('node:module');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const runtime = process.env.NEXUS_BUNDLED_PLAYWRIGHT_ROOT;
const evidencePath = process.env.NEXUS_PLAYWRIGHT_SUITE_RECEIPT;
const evidence = globalThis.__nexusSuiteEvidence = {
  schema_version: 1, worker_mode: 'IN_PROCESS_WORKER_MAIN',
  tests: [], steps: [], requests: [], tls: [], final_urls: [], api: [],
  external_write_denied: false, worker_ready: false, worker_exited: false
};
function writeEvidence() {
  try { fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2) + '\n'); } catch {}
}
process.on('beforeExit', writeEvidence); process.on('exit', writeEvidence);
try { fs.writeFileSync(process.env.NEXUS_PLAYWRIGHT_DENIED_WRITE, 'escape'); }
catch (error) { evidence.external_write_denied = ['EACCES', 'EPERM'].includes(error.code); }

// The project process can reach only the two engine-owned same-profile
// loopback endpoints. Browser traffic is separately forced through the exact
// CONNECT proxy; direct LAN/Internet sockets from suite code fail closed.
const allowedPorts = new Set(String(process.env.NEXUS_ALLOWED_LOOPBACK_PORTS || '').split(',').filter(Boolean).map(Number));
const originalConnect = net.connect.bind(net);
function guardedConnect(...args) {
  let host = 'localhost', port = 0;
  if (typeof args[0] === 'object') { host = args[0].host || args[0].hostname || host; port = Number(args[0].port); }
  else { port = Number(args[0]); if (typeof args[1] === 'string') host = args[1]; }
  const folded = String(host).toLowerCase();
  if (!['localhost', '127.0.0.1', '::1'].includes(folded) || !allowedPorts.has(port)) {
    const error = new Error('Nexus exact-origin containment denied direct network socket');
    error.code = 'EACCES'; throw error;
  }
  return originalConnect(...args);
}
net.connect = guardedConnect; net.createConnection = guardedConnect;

const playwright = require(path.join(runtime, 'node_modules', 'playwright'));
const originalConnectOverCDP = playwright.chromium.connectOverCDP.bind(playwright.chromium);
async function instrumentBrowser(browser) {
  const originalNewContext = browser.newContext.bind(browser);
  browser.newContext = async options => instrumentContext(await originalNewContext(options));
  for (const context of browser.contexts()) instrumentContext(context);
  return browser;
}
function instrumentContext(context) {
  if (context.__nexusInstrumented) return context;
  Object.defineProperty(context, '__nexusInstrumented', {value: true});
  const instrumentPage = page => {
    if (page.__nexusInstrumented) return;
    Object.defineProperty(page, '__nexusInstrumented', {value: true});
    page.on('request', request => evidence.requests.push({url: request.url(), method: request.method(), resource_type: request.resourceType()}));
    page.on('response', async response => {
      try { const details = await response.securityDetails(); if (details) evidence.tls.push({url: response.url(), status: response.status(), ...details}); } catch {}
    });
    page.on('close', () => { try { evidence.final_urls.push(page.url()); } catch {} });
  };
  context.pages().forEach(instrumentPage); context.on('page', instrumentPage);
  return context;
}
playwright.chromium.launch = async () => instrumentBrowser(await originalConnectOverCDP(process.env.NEXUS_CDP_ENDPOINT));
playwright.chromium.launchPersistentContext = async () => { throw new Error('Nexus broker disallows project persistent browser authority'); };
for (const name of ['firefox', 'webkit']) {
  playwright[name].launch = async () => { throw new Error('Nexus bundled verification supports brokered Chromium only'); };
}
const originalRequestNewContext = playwright.request.newContext.bind(playwright.request);
playwright.request.newContext = async options => {
  const requested = options && options.baseURL ? new URL(options.baseURL).origin : process.env.NEXUS_APPROVED_ORIGIN;
  if (requested !== process.env.NEXUS_APPROVED_ORIGIN) throw new Error('Nexus denied cross-origin API request context');
  const context = await originalRequestNewContext({...options, proxy: {server: process.env.NEXUS_ORIGIN_PROXY}});
  for (const method of ['fetch', 'get', 'head']) {
    const original = context[method].bind(context);
    context[method] = async (...args) => {
      const response = await original(...args);
      evidence.api.push({method: method.toUpperCase(), url: response.url(), status: response.status()});
      return response;
    };
  }
  return context;
};

function loadWorkerCreate(entry) {
  let source = fs.readFileSync(entry, 'utf8');
  const marker = '(0, import_common4.startProcessRunner)(create);';
  if (!source.includes(marker)) throw new Error('Pinned Playwright WorkerMain marker is unavailable');
  source = source.replace(marker, 'module.exports = { create };');
  const workerModule = new Module(entry + '.nexus-inprocess', module);
  workerModule.filename = entry;
  workerModule.paths = Module._nodeModulePaths(path.dirname(entry));
  workerModule._compile(source, entry);
  return workerModule.exports.create;
}
class InProcessWorker extends EventEmitter {
  constructor(entry, options) {
    super(); this.stdout = new PassThrough(); this.stderr = new PassThrough();
    this.pid = process.pid; this.connected = true; this._runner = null;
    this._create = loadWorkerCreate(entry); this._env = options && options.env || process.env;
    setImmediate(() => { evidence.worker_ready = true; this.emit('message', {method: 'ready'}); });
  }
  send(message) {
    if (message.method === '__init__') {
      Object.assign(process.env, this._env);
      const out = process.stdout.write, err = process.stderr.write;
      this._runner = this._create(message.params.runnerParams);
      process.stdout.write = out; process.stderr.write = err;
      return true;
    }
    if (message.method === '__stop__') {
      Promise.resolve(this._runner && this._runner.gracefullyClose()).finally(() => {
        this.emit('message', {method: '__env_produced__', params: []});
        this.connected = false; evidence.worker_exited = true; writeEvidence();
        this.emit('exit', 0, null);
      });
      return true;
    }
    if (message.method === '__dispatch__') {
      const {id, method, params} = message.params;
      Promise.resolve().then(() => this._runner[method](params)).then(result => {
        this.emit('message', {method: '__dispatch__', params: {id, result}});
      }, error => this.emit('message', {method: '__dispatch__', params: {id, error: {message: String(error.message || error), stack: error.stack}}}));
      return true;
    }
    return true;
  }
  kill() { this.send({method: '__stop__'}); return true; }
}
const originalFork = cp.fork;
const stepIndexes = new Map();
cp.fork = function(entry, argsOrOptions, maybeOptions) {
  if (!String(entry).endsWith('workerProcessEntry.js'))
    throw new Error('Nexus contained Playwright denied an unbrokered child process: ' + entry);
  const options = Array.isArray(argsOrOptions) ? maybeOptions || {} : argsOrOptions || {};
  const worker = new InProcessWorker(entry, options);
  const previousSend = process.send;
  process.send = message => {
    if (message && message.method === '__dispatch__') {
      const payload = message.params || {};
      if (payload.method === 'testBegin') evidence.tests.push({testId: payload.params.testId, status: 'running'});
      if (payload.method === 'testEnd') {
        const found = evidence.tests.find(one => one.testId === payload.params.testId);
        if (found) Object.assign(found, {status: payload.params.status, expectedStatus: payload.params.expectedStatus, errors: payload.params.errors});
      }
      if (payload.method === 'stepBegin') {
        stepIndexes.set(payload.params.stepId, evidence.steps.length);
        evidence.steps.push({title: payload.params.title, category: payload.params.category, error: null});
      }
      if (payload.method === 'stepEnd') {
        const index = stepIndexes.get(payload.params.stepId);
        if (index !== undefined) evidence.steps[index].error = payload.params.error || null;
      }
    }
    setImmediate(() => worker.emit('message', message)); return true;
  };
  worker.once('exit', () => { if (process.send === worker._send) process.send = previousSend; });
  return worker;
};
'''


_UNMODIFIED_SUITE_RUNNER_SOURCE = r'''
const path = require('node:path');
require(process.env.NEXUS_INPROCESS_WORKER_SHIM);
const args = JSON.parse(process.env.NEXUS_PLAYWRIGHT_CLI_ARGS);
process.argv = [process.execPath, process.env.NEXUS_PLAYWRIGHT_CLI, ...args];
require(process.env.NEXUS_PLAYWRIGHT_CLI);
'''


_LOCATOR_KINDS = {
    "getByRole", "getByText", "getByLabel", "getByPlaceholder",
    "getByTestId", "locator",
}
_SCENARIO_ACTIONS = {
    "goto", "click", "fill", "press", "keyboard", "check", "uncheck",
    "selectOption", "focus", "waitFor", "assert", "api",
}
_ASSERTIONS = {"visible", "hidden", "url", "text", "value", "attribute", "count"}


def _literal_locator_expression(expression: str) -> dict[str, Any] | None:
    """Parse one common literal locator chain without evaluating project code."""

    matched = re.fullmatch(
        r"\s*page\s*\.\s*(?P<kind>getByRole|getByText|getByLabel|getByPlaceholder|getByTestId|locator)"
        r"\s*\(\s*(['\"])(?P<first>.*?)\2(?P<options>.*?)\)"
        r"(?P<chain>(?:\s*\.\s*(?:filter|nth)\s*\([^\r\n;]*?\))*)\s*",
        expression, re.S,
    )
    if matched is None:
        return None
    kind = matched.group("kind")
    options = matched.group("options").strip()
    locator: dict[str, Any] = {"kind": kind}
    if kind == "getByRole":
        locator["role"] = matched.group("first")
        if options:
            if not options.startswith(","):
                return None
            body = options[1:].strip()
            if not (body.startswith("{") and body.endswith("}")):
                return None
            name = re.search(r"\bname\s*:\s*(['\"])(.*?)\1", body, re.S)
            exact = re.search(r"\bexact\s*:\s*(true|false)\b", body, re.I)
            residue = re.sub(r"\b(?:name\s*:\s*(['\"])(.*?)\1|exact\s*:\s*(?:true|false))\s*,?", "", body[1:-1], flags=re.I | re.S).strip()
            if residue:
                return None
            if name:
                locator["name"] = name.group(2)
            if exact:
                locator["exact"] = exact.group(1).casefold() == "true"
    else:
        if options:
            if kind not in {"getByText", "getByLabel", "getByPlaceholder"} or not options.startswith(","):
                return None
            exact = re.fullmatch(r",\s*\{\s*exact\s*:\s*(true|false)\s*\}", options, re.I)
            if exact is None:
                return None
            locator["exact"] = exact.group(1).casefold() == "true"
        locator["selector" if kind == "locator" else "value"] = matched.group("first")
    chain = matched.group("chain")
    filtering = re.search(r"\.\s*filter\s*\(\s*\{(?P<body>.*?)\}\s*\)", chain, re.S)
    if filtering:
        values: dict[str, str] = {}
        for key, _quote, value in re.findall(
            r"\b(hasText|hasNotText)\s*:\s*(['\"])(.*?)\2", filtering.group("body"), re.S,
        ):
            values[key] = value
        residue = re.sub(
            r"\b(?:hasText|hasNotText)\s*:\s*(['\"])(.*?)\1\s*,?", "",
            filtering.group("body"), flags=re.S,
        ).strip()
        if not values or residue:
            return None
        locator["filter"] = values
    nth = re.search(r"\.\s*nth\s*\(\s*(-?\d+)\s*\)", chain)
    if nth:
        locator["nth"] = int(nth.group(1))
    return locator


def extract_safe_playwright_scenario(
    source: str, approved_base_url: str,
) -> dict[str, Any] | None:
    """Extract a conservative common async suite into validated data-only IR.

    Literal locator aliases are supported. Project functions, callbacks,
    evaluation, arbitrary request bodies, and unrecognized browser assertions
    fail closed instead of being executed as an oracle.
    """

    if not isinstance(source, str) or re.search(
        r"\b(?:eval|Function|child_process|exec|spawn|page\s*\.\s*evaluate)\b", source,
    ):
        return None
    aliases: dict[str, dict[str, Any]] = {}
    for match in re.finditer(
        r"\b(?:const|let)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*"
        r"(?P<locator>page\s*\.\s*(?:getByRole|getByText|getByLabel|getByPlaceholder|getByTestId|locator)[^;\r\n]+)\s*;?",
        source,
    ):
        locator = _literal_locator_expression(match.group("locator").strip())
        if locator is None:
            return None
        aliases[match.group("name")] = locator

    def locator(value: str) -> dict[str, Any] | None:
        stripped = value.strip()
        return dict(aliases[stripped]) if stripped in aliases else _literal_locator_expression(stripped)

    events: list[tuple[int, dict[str, Any]]] = []
    for match in re.finditer(
        r"\b(?:await\s+)?page\s*\.\s*goto\s*\(\s*(['\"])(?P<url>.*?)\1\s*\)", source, re.S,
    ):
        events.append((match.start(), {"op": "goto", "url": match.group("url")}))

    target_pattern = (
        r"(?P<target>page\s*\.\s*(?:getByRole|getByText|getByLabel|getByPlaceholder|getByTestId|locator)"
        r"[^;\r\n]+?|[A-Za-z_$][\w$]*)"
    )
    action_pattern = re.compile(
        r"\bawait\s+" + target_pattern
        + r"\s*\.\s*(?P<op>click|fill|press|check|uncheck|selectOption|focus|waitFor)\s*"
          r"\(\s*(?P<args>[^\r\n;]*?)\s*\)\s*;?",
    )
    for match in action_pattern.finditer(source):
        target = locator(match.group("target"))
        if target is None:
            return None
        op = match.group("op")
        args = match.group("args").strip()
        step: dict[str, Any] = {"op": op, "target": target}
        if op in {"fill", "press"}:
            value = re.fullmatch(r"(['\"])(.*?)\1", args, re.S)
            if value is None:
                return None
            step["value" if op == "fill" else "key"] = value.group(2)
        elif op == "selectOption":
            values = re.findall(r"(['\"])(.*?)\1", args, re.S)
            if not values:
                return None
            step["values"] = [one[1] for one in values]
        elif op == "waitFor":
            state = re.fullmatch(r"\{\s*state\s*:\s*(['\"])(attached|detached|visible|hidden)\1\s*\}", args)
            if state is None:
                return None
            step["state"] = state.group(2)
        elif args:
            return None
        events.append((match.start(), step))

    for match in re.finditer(
        r"\bawait\s+page\s*\.\s*keyboard\s*\.\s*(?P<action>type|press|insertText)"
        r"\s*\(\s*(['\"])(?P<value>.*?)\2\s*\)", source, re.S,
    ):
        events.append((match.start(), {
            "op": "keyboard", "action": match.group("action"), "value": match.group("value"),
        }))

    assertion_pattern = re.compile(
        r"\bawait\s+expect\s*\(\s*" + target_pattern + r"\s*\)\s*\.\s*"
        r"(?P<matcher>toBeVisible|toBeHidden|toHaveText|toHaveValue|toHaveAttribute|toHaveCount)"
        r"\s*\(\s*(?P<args>[^\r\n;]*?)\s*\)",
    )
    assertion_matches = list(assertion_pattern.finditer(source))
    for match in assertion_matches:
        target = locator(match.group("target"))
        if target is None:
            return None
        matcher = match.group("matcher")
        condition = {
            "toBeVisible": "visible", "toBeHidden": "hidden", "toHaveText": "text",
            "toHaveValue": "value", "toHaveAttribute": "attribute", "toHaveCount": "count",
        }[matcher]
        step = {"op": "assert", "condition": condition, "target": target}
        args = match.group("args").strip()
        if condition in {"visible", "hidden"}:
            if args:
                return None
        elif condition == "count":
            if not re.fullmatch(r"\d+", args):
                return None
            step["expected"] = int(args)
        else:
            strings = re.findall(r"(['\"])(.*?)\1", args, re.S)
            if condition == "attribute" and len(strings) == 2:
                step.update({"name": strings[0][1], "expected": strings[1][1]})
            elif condition != "attribute" and len(strings) == 1:
                step["expected"] = strings[0][1]
            else:
                return None
        events.append((match.start(), step))
    for match in re.finditer(
        r"\bawait\s+expect\s*\(\s*page\s*\)\s*\.\s*toHaveURL\s*"
        r"\(\s*(['\"])(?P<url>.*?)\1\s*\)", source, re.S,
    ):
        events.append((match.start(), {"op": "assert", "condition": "url", "expected": match.group("url")}))
    # Every awaited browser assertion must be represented. This prevents a
    # partial subset from being reported as the suite result.
    browser_expects = len(re.findall(r"\bawait\s+expect\s*\(", source))
    parsed_expects = len(assertion_matches) + len(re.findall(
        r"\bawait\s+expect\s*\(\s*page\s*\)\s*\.\s*toHaveURL", source,
    ))
    if browser_expects != parsed_expects:
        return None
    events.sort(key=lambda one: one[0])
    if not events:
        return None
    extracted = {
        "base_url": approved_base_url,
        "steps": [one[1] for one in events],
    }
    try:
        validate_safe_playwright_scenario(extracted, approved_base_url)
    except ValueError:
        return None
    return extracted


def _bounded_string(
    value: object, label: str, *, limit: int = 4096, allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str) or (not value and not allow_empty)
        or len(value) > limit or "\x00" in value
    ):
        qualifier = "bounded" if allow_empty else "non-empty bounded"
        raise ValueError(f"{label} must be a {qualifier} string")
    return value


def _exact_origin_url(base_url: str, value: object, origin: str, label: str) -> str:
    raw = _bounded_string(value, label)
    target = urljoin(base_url, raw)
    parsed = urlsplit(target)
    target_origin = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
    if target_origin != origin.casefold():
        raise ValueError(f"{label} must stay on the exact approved origin {origin}")
    return target


def _validated_locator(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "kind", "role", "name", "value", "selector", "exact", "filter", "nth",
    }:
        raise ValueError(f"{label} has unsupported locator fields")
    kind = value.get("kind")
    if kind not in _LOCATOR_KINDS:
        raise ValueError(f"{label} has an unsupported locator kind")
    output: dict[str, Any] = {"kind": kind}
    if kind == "getByRole":
        output["role"] = _bounded_string(value.get("role"), label + ".role", limit=100)
        if "name" in value:
            output["name"] = _bounded_string(value["name"], label + ".name")
    elif kind == "locator":
        output["selector"] = _bounded_string(value.get("selector"), label + ".selector", limit=500)
    else:
        output["value"] = _bounded_string(value.get("value"), label + ".value")
    if "exact" in value:
        if not isinstance(value["exact"], bool):
            raise ValueError(f"{label}.exact must be boolean")
        output["exact"] = value["exact"]
    if "filter" in value:
        filtering = value["filter"]
        if not isinstance(filtering, Mapping) or set(filtering) - {"hasText", "hasNotText"} or not filtering:
            raise ValueError(f"{label}.filter supports only hasText/hasNotText")
        output["filter"] = {
            key: _bounded_string(one, f"{label}.filter.{key}")
            for key, one in filtering.items()
        }
    if "nth" in value:
        nth = value["nth"]
        if not isinstance(nth, int) or isinstance(nth, bool) or nth < -1:
            raise ValueError(f"{label}.nth must be -1 or a non-negative integer")
        output["nth"] = nth
    return output


def validate_safe_playwright_scenario(
    scenario: Mapping[str, Any], approved_base_url: str,
) -> dict[str, Any]:
    """Validate a data-only common Playwright scenario; never accept code."""

    if not isinstance(scenario, Mapping) or set(scenario) - {"base_url", "config", "steps"}:
        raise ValueError("Playwright scenario has unsupported top-level fields")
    approved_base, approved_origin, _host, _port = normalize_approved_https_base_url(
        approved_base_url
    )
    config = scenario.get("config", {})
    if not isinstance(config, Mapping) or set(config) - {"baseURL", "timeout_ms", "test_id_attribute"}:
        raise ValueError("Playwright scenario config has unsupported fields")
    supplied_base = scenario.get("base_url", config.get("baseURL"))
    if not isinstance(supplied_base, str):
        raise ValueError("Playwright scenario must declare base_url or config.baseURL")
    scenario_base, scenario_origin, _scenario_host, _scenario_port = normalize_approved_https_base_url(
        supplied_base
    )
    if scenario_base != approved_base or scenario_origin != approved_origin:
        raise ValueError("Playwright scenario baseURL does not exactly match the approved baseURL")
    if "base_url" in scenario and "baseURL" in config:
        config_base, _one, _two, _three = normalize_approved_https_base_url(str(config["baseURL"]))
        if config_base != scenario_base:
            raise ValueError("Playwright base_url and config.baseURL disagree")
    timeout_ms = config.get("timeout_ms", 10_000)
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 100 <= timeout_ms <= 120_000:
        raise ValueError("Playwright scenario timeout_ms must be between 100 and 120000")
    test_id_attribute = config.get("test_id_attribute", "data-testid")
    if not isinstance(test_id_attribute, str) or not test_id_attribute or len(test_id_attribute) > 100:
        raise ValueError("Playwright test_id_attribute is invalid")
    raw_steps = scenario.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 200:
        raise ValueError("Playwright scenario must contain 1 to 200 steps")
    steps: list[dict[str, Any]] = []
    saw_goto = False
    assertion_count = 0
    for index, raw in enumerate(raw_steps):
        label = f"steps[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(label + " must be an object")
        op = raw.get("op")
        if op not in _SCENARIO_ACTIONS:
            raise ValueError(label + " has an unsupported operation")
        allowed = {"op"}
        step: dict[str, Any] = {"op": op}
        if op == "goto":
            allowed |= {"url"}
            step["url"] = _exact_origin_url(scenario_base, raw.get("url"), approved_origin, label + ".url")
            saw_goto = True
        elif op == "keyboard":
            allowed |= {"action", "value", "target"}
            action = raw.get("action")
            if action not in {"type", "press", "insertText"}:
                raise ValueError(label + ".action is unsupported")
            step["action"] = action
            step["value"] = _bounded_string(raw.get("value"), label + ".value")
            if "target" in raw:
                step["target"] = _validated_locator(raw["target"], label + ".target")
        elif op == "assert":
            allowed |= {"condition", "target", "expected", "name"}
            condition = raw.get("condition")
            if condition not in _ASSERTIONS:
                raise ValueError(label + ".condition is unsupported")
            step["condition"] = condition
            if condition == "url":
                step["expected"] = _exact_origin_url(
                    scenario_base, raw.get("expected"), approved_origin, label + ".expected"
                )
            else:
                step["target"] = _validated_locator(raw.get("target"), label + ".target")
                if condition in {"visible", "hidden"}:
                    expected = raw.get("expected", True)
                    if not isinstance(expected, bool):
                        raise ValueError(label + ".expected must be boolean")
                    step["expected"] = expected
                elif condition == "count":
                    expected = raw.get("expected")
                    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                        raise ValueError(label + ".expected count must be a non-negative integer")
                    step["expected"] = expected
                else:
                    step["expected"] = _bounded_string(
                        raw.get("expected"), label + ".expected", allow_empty=True,
                    )
                if condition == "attribute":
                    step["name"] = _bounded_string(raw.get("name"), label + ".name", limit=200)
            assertion_count += 1
        elif op == "api":
            allowed |= {"method", "url", "expected_status", "expected_text"}
            method = str(raw.get("method", "GET")).upper()
            if method not in {"GET", "HEAD"}:
                raise ValueError(label + " API method is not read-only")
            step["method"] = method
            step["url"] = _exact_origin_url(scenario_base, raw.get("url"), approved_origin, label + ".url")
            status = raw.get("expected_status", 200)
            if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                raise ValueError(label + ".expected_status is invalid")
            step["expected_status"] = status
            if "expected_text" in raw:
                step["expected_text"] = _bounded_string(raw["expected_text"], label + ".expected_text")
            assertion_count += 1
        else:
            allowed |= {"target", "value", "key", "values", "state"}
            step["target"] = _validated_locator(raw.get("target"), label + ".target")
            if op == "fill":
                step["value"] = str(raw.get("value", ""))
                if len(step["value"]) > 16_384 or "\x00" in step["value"]:
                    raise ValueError(label + ".value is too large")
            elif op == "press":
                step["key"] = _bounded_string(raw.get("key"), label + ".key", limit=100)
            elif op == "selectOption":
                values = raw.get("values", raw.get("value"))
                if isinstance(values, str):
                    values = [values]
                if not isinstance(values, list) or not values or len(values) > 50:
                    raise ValueError(label + ".values must be a bounded string list")
                step["values"] = [_bounded_string(one, label + ".values") for one in values]
            elif op == "waitFor":
                state = raw.get("state", "visible")
                if state not in {"attached", "detached", "visible", "hidden"}:
                    raise ValueError(label + ".state is unsupported")
                step["state"] = state
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(label + " has unsupported fields: " + ", ".join(sorted(unknown)))
        steps.append(step)
    if not saw_goto:
        raise ValueError("Playwright scenario must navigate to the exact approved origin")
    if assertion_count == 0:
        raise ValueError("Playwright scenario must contain at least one assertion")
    return {
        "schema_version": 1,
        "base_url": scenario_base,
        "origin": approved_origin,
        "config": {"timeout_ms": timeout_ms, "test_id_attribute": test_id_attribute},
        "steps": steps,
    }


def compile_safe_playwright_scenario(
    scenario: Mapping[str, Any], approved_base_url: str,
) -> tuple[dict[str, Any], str]:
    """Compile validated data to one engine-owned asynchronous Node runner."""

    validated = validate_safe_playwright_scenario(scenario, approved_base_url)
    encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
    source = r'''
const fs = require('node:fs');
const path = require('node:path');
const pw = require(path.join(process.env.NEXUS_BUNDLED_PLAYWRIGHT_ROOT, 'node_modules', 'playwright'));
const scenario = JSON.parse(__NEXUS_SCENARIO__);
const receiptPath = process.env.NEXUS_PLAYWRIGHT_RECEIPT_PATH;
const receipt = {
  schema_version: 1,
  configured_base_url: scenario.base_url,
  configured_origin: scenario.origin,
  final_url: '', final_origin: '', tls: [], request_routes: [],
  actions: [], assertions: [], api: [], external_write_denied: false,
  passed: false, error: ''
};
function writeReceipt() { fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2) + '\n'); }
function exactOrigin(value) { return new URL(value).origin === scenario.origin; }
function target(page, spec) {
  let located;
  if (spec.kind === 'getByRole') located = page.getByRole(spec.role, {name: spec.name, exact: spec.exact});
  else if (spec.kind === 'getByText') located = page.getByText(spec.value, {exact: spec.exact});
  else if (spec.kind === 'getByLabel') located = page.getByLabel(spec.value, {exact: spec.exact});
  else if (spec.kind === 'getByPlaceholder') located = page.getByPlaceholder(spec.value, {exact: spec.exact});
  else if (spec.kind === 'getByTestId') located = page.getByTestId(spec.value);
  else if (spec.kind === 'locator') located = page.locator(spec.selector);
  else throw new Error('unsupported validated locator');
  if (spec.filter) located = located.filter(spec.filter);
  if (spec.nth !== undefined) located = located.nth(spec.nth);
  return located;
}
async function assertStep(page, step, index) {
  let actual;
  if (step.condition === 'url') actual = page.url();
  else {
    const located = target(page, step.target);
    if (step.condition === 'visible') actual = await located.isVisible();
    else if (step.condition === 'hidden') actual = await located.isHidden();
    else if (step.condition === 'text') actual = await located.textContent();
    else if (step.condition === 'value') actual = await located.inputValue();
    else if (step.condition === 'attribute') actual = await located.getAttribute(step.name);
    else if (step.condition === 'count') actual = await located.count();
    else throw new Error('unsupported validated assertion');
  }
  const passed = actual === step.expected;
  receipt.assertions.push({index, condition: step.condition, expected: step.expected, actual, passed});
  if (!passed) throw new Error(`assertion ${index} ${step.condition} expected ${JSON.stringify(step.expected)} got ${JSON.stringify(actual)}`);
}
(async () => {
  if (process.env.NEXUS_APPROVED_BASE_URL !== scenario.base_url || process.env.NEXUS_APPROVED_ORIGIN !== scenario.origin)
    throw new Error('broker/scenario exact-origin binding mismatch');
  if (!receiptPath) throw new Error('receipt path is unavailable');
  try { fs.writeFileSync(process.env.NEXUS_PLAYWRIGHT_DENIED_WRITE, 'escape'); }
  catch (error) { receipt.external_write_denied = ['EACCES', 'EPERM'].includes(error.code); }
  if (!receipt.external_write_denied) throw new Error('AppContainer external-write denial was not enforced');
  pw.selectors.setTestIdAttribute(scenario.config.test_id_attribute);
  const browser = await pw.chromium.connectOverCDP(process.env.NEXUS_CDP_ENDPOINT);
  const context = browser.contexts()[0];
  if (!context) throw new Error('brokered Chromium has no default context');
  context.setDefaultTimeout(scenario.config.timeout_ms);
  context.setDefaultNavigationTimeout(scenario.config.timeout_ms);
  const page = context.pages()[0] || await context.newPage();
  page.on('request', request => receipt.request_routes.push({
    event: 'request', url: request.url(), method: request.method(), resource_type: request.resourceType(),
    exact_origin: (() => { try { return exactOrigin(request.url()); } catch { return false; } })()
  }));
  page.on('requestfailed', request => receipt.request_routes.push({
    event: 'requestfailed', url: request.url(), failure: request.failure()?.errorText || 'unknown'
  }));
  let apiContext = null;
  for (let index = 0; index < scenario.steps.length; index++) {
    const step = scenario.steps[index];
    if (step.op === 'goto') {
      const response = await page.goto(step.url, {waitUntil: 'domcontentloaded'});
      if (!response) throw new Error('navigation produced no HTTP response');
      const finalUrl = page.url();
      if (!exactOrigin(finalUrl)) throw new Error('navigation left the exact approved origin: ' + finalUrl);
      const security = await response.securityDetails();
      if (!security || !String(security.protocol || '').toUpperCase().startsWith('TLS'))
        throw new Error('navigation did not prove a validated TLS route');
      receipt.tls.push({url: response.url(), status: response.status(), ...security});
      receipt.actions.push({index, op: step.op, configured_url: step.url, final_url: finalUrl});
    } else if (step.op === 'assert') {
      await assertStep(page, step, index);
    } else if (step.op === 'api') {
      if (!apiContext) apiContext = await pw.request.newContext({
        baseURL: scenario.base_url,
        proxy: {server: process.env.NEXUS_ORIGIN_PROXY},
        ignoreHTTPSErrors: false,
      });
      const response = await apiContext.fetch(step.url, {method: step.method});
      const body = step.method === 'HEAD' ? '' : await response.text();
      const statusPassed = response.status() === step.expected_status;
      const textPassed = step.expected_text === undefined || body.includes(step.expected_text);
      receipt.api.push({index, url: response.url(), method: step.method, status: response.status(), status_passed: statusPassed, text_passed: textPassed});
      receipt.assertions.push({index, condition: 'api', expected_status: step.expected_status, actual_status: response.status(), passed: statusPassed && textPassed});
      if (!exactOrigin(response.url()) || !statusPassed || !textPassed) throw new Error('exact-origin API assertion failed at step ' + index);
    } else if (step.op === 'keyboard') {
      if (step.target) await target(page, step.target).focus();
      if (step.action === 'type') await page.keyboard.type(step.value);
      else if (step.action === 'press') await page.keyboard.press(step.value);
      else await page.keyboard.insertText(step.value);
      receipt.actions.push({index, op: step.op, action: step.action});
    } else {
      const located = target(page, step.target);
      if (step.op === 'click') await located.click();
      else if (step.op === 'fill') await located.fill(step.value);
      else if (step.op === 'press') await located.press(step.key);
      else if (step.op === 'check') await located.check();
      else if (step.op === 'uncheck') await located.uncheck();
      else if (step.op === 'selectOption') await located.selectOption(step.values);
      else if (step.op === 'focus') await located.focus();
      else if (step.op === 'waitFor') await located.waitFor({state: step.state});
      else throw new Error('unsupported validated action');
      receipt.actions.push({index, op: step.op});
    }
  }
  if (apiContext) await apiContext.dispose();
  receipt.final_url = page.url();
  receipt.final_origin = new URL(receipt.final_url).origin;
  if (receipt.final_origin !== scenario.origin) throw new Error('final page origin differs from configured origin');
  receipt.passed = receipt.assertions.length > 0 && receipt.assertions.every(one => one.passed === true);
  writeReceipt();
  await browser.close();
  if (!receipt.passed) process.exit(1);
})().catch(async error => {
  receipt.error = String(error && error.stack || error);
  try { writeReceipt(); } catch {}
  process.exit(1);
});
'''.replace("__NEXUS_SCENARIO__", json.dumps(encoded, ensure_ascii=False))
    return validated, source


def run_safe_playwright_scenario(
    snapshot: Path,
    scenario: Mapping[str, Any],
    approved_base_url: str,
    *,
    timeout: float = 45.0,
    runtime: BundledPlaywrightRuntime | None = None,
) -> dict[str, Any]:
    """Run a validated common scenario and bind DOM/TLS/routes to its origin."""

    validated, source = compile_safe_playwright_scenario(scenario, approved_base_url)
    snapshot = snapshot.resolve()
    if not snapshot.is_dir():
        raise ValueError("Playwright scenario snapshot must exist")
    control = snapshot / ".nexus-verification"
    control.mkdir(parents=True, exist_ok=True)
    runner = control / "exact-origin-scenario.cjs"
    receipt_path = control / "exact-origin-receipt.json"
    denied_path = snapshot.parent / ("nexus-playwright-denied-" + secrets.token_hex(16) + ".txt")
    receipt_path.unlink(missing_ok=True)
    runner.write_text(source, encoding="utf-8")
    broker = run_brokered_playwright_appcontainer(
        snapshot, runner, timeout=timeout, runtime=runtime,
        approved_base_url=validated["base_url"],
        environment={
            "NEXUS_PLAYWRIGHT_RECEIPT_PATH": str(receipt_path),
            "NEXUS_PLAYWRIGHT_DENIED_WRITE": str(denied_path),
        },
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        receipt = None
    exact_document_routes = []
    if isinstance(receipt, dict) and isinstance(receipt.get("request_routes"), list):
        exact_document_routes = [
            one for one in receipt["request_routes"]
            if isinstance(one, dict) and one.get("event") == "request"
            and one.get("resource_type") == "document" and one.get("exact_origin") is True
        ]
    receipt_ok = bool(
        isinstance(receipt, dict)
        and receipt.get("passed") is True
        and receipt.get("configured_base_url") == validated["base_url"]
        and receipt.get("configured_origin") == validated["origin"]
        and receipt.get("final_origin") == validated["origin"]
        and receipt.get("external_write_denied") is True
        and not denied_path.exists()
        and isinstance(receipt.get("tls"), list) and receipt["tls"]
        and all(
            isinstance(one, dict)
            and str(one.get("protocol", "")).upper().startswith("TLS")
            and urlsplit(str(one.get("url", ""))).scheme.casefold() == "https"
            for one in receipt["tls"]
        )
        and isinstance(receipt.get("assertions"), list) and receipt["assertions"]
        and all(isinstance(one, dict) and one.get("passed") is True for one in receipt["assertions"])
        and exact_document_routes
        and broker.get("exact_origin_route_attested") is True
    )
    evidence_receipt = {
        "schema_version": 1,
        "route_mode": "REMOTE_TLS_TUNNEL",
        "configured_base_url": validated["base_url"],
        "configured_origin": validated["origin"],
        "final_url": receipt.get("final_url") if isinstance(receipt, dict) else None,
        "final_origin": receipt.get("final_origin") if isinstance(receipt, dict) else None,
        "tls": receipt.get("tls", []) if isinstance(receipt, dict) else [],
        "browser_request_routes": receipt.get("request_routes", []) if isinstance(receipt, dict) else [],
        "dom_assertions": receipt.get("assertions", []) if isinstance(receipt, dict) else [],
        "api_assertions": receipt.get("api", []) if isinstance(receipt, dict) else [],
        "origin_proxy_routes": broker.get("origin_routes", []),
        "exact_origin_route_attested": broker.get("exact_origin_route_attested") is True,
        "cdp_endpoint_scope": broker.get("endpoint_scope"),
        "appcontainer_profile": broker.get("profile"),
        "process_capabilities": broker.get("process_capabilities"),
        "boundary_inheritance_attested": broker.get("boundary_inheritance_attested") is True,
        "external_write_authority": broker.get("external_write_authority"),
        "external_write_denied": (
            isinstance(receipt, dict) and receipt.get("external_write_denied") is True
            and not denied_path.exists()
        ),
        "passed": bool(broker.get("passed") and receipt_ok),
    }
    return {
        "schema_version": 1,
        "passed": bool(broker.get("passed") and receipt_ok),
        "validated_scenario": validated,
        "receipt": receipt,
        "evidence_receipt": evidence_receipt,
        "receipt_attested": receipt_ok,
        "broker": broker,
    }


def run_brokered_playwright_suite(
    snapshot: Path,
    cli_args: Sequence[str],
    approved_base_url: str,
    *,
    environment: Mapping[str, str] | None = None,
    timeout: float = 90.0,
    runtime: BundledPlaywrightRuntime | None = None,
) -> dict[str, Any]:
    """Execute the selected, unmodified Playwright suite with in-process WorkerMain."""

    approved_base, approved_origin, _host, _port = normalize_approved_https_base_url(
        approved_base_url
    )
    runtime = runtime or discover_bundled_playwright_runtime(required=True)
    assert runtime is not None
    snapshot = snapshot.resolve()
    if not snapshot.is_dir():
        raise ValueError("Playwright suite snapshot must exist")
    control = snapshot / ".nexus-verification" / "unmodified-playwright-suite"
    control.mkdir(parents=True, exist_ok=True)
    shim = control / "inprocess-worker-shim.cjs"
    runner = control / "suite-runner.cjs"
    receipt_path = control / "suite-receipt.json"
    receipt_path.unlink(missing_ok=True)
    denied_path = snapshot.parent / ("nexus-suite-denied-" + secrets.token_hex(16) + ".txt")
    shim.write_text(_INPROCESS_PLAYWRIGHT_WORKER_SHIM_SOURCE, encoding="utf-8")
    runner.write_text(_UNMODIFIED_SUITE_RUNNER_SOURCE, encoding="utf-8")
    node_workspace = snapshot / ".nexus-verification" / "node-workspace"
    if node_workspace.exists():
        shutil.rmtree(node_workspace)
    node_workspace.mkdir(parents=True)
    (node_workspace / ".nexus-verification").mkdir()
    for child in snapshot.iterdir():
        if child.name == ".nexus-verification":
            continue
        destination = node_workspace / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, destination, symlinks=True)
        elif child.is_file() and not child.is_symlink():
            shutil.copy2(child, destination)
    args = [str(one) for one in cli_args]
    if not args or args[0] != "test":
        args.insert(0, "test")
    if not any(one == "--workers" or one.startswith("--workers=") for one in args):
        args.append("--workers=1")
    if not any(one == "--reporter" or one.startswith("--reporter=") for one in args):
        args.append("--reporter=line")
    broker = run_brokered_playwright_appcontainer(
        snapshot, runner, timeout=timeout, runtime=runtime,
        approved_base_url=approved_base,
        runner_nested_workspace=True,
        environment={
            **dict(environment or {}),
            "NEXUS_INPROCESS_WORKER_SHIM": str(shim),
            "NEXUS_PLAYWRIGHT_CLI": str(runtime.cli),
            "NEXUS_PLAYWRIGHT_CLI_ARGS": json.dumps(args, separators=(",", ":")),
            "NEXUS_PLAYWRIGHT_SUITE_RECEIPT": str(receipt_path),
            "NEXUS_PLAYWRIGHT_DENIED_WRITE": str(denied_path),
            "NEXUS_APPROVED_BASE_URL": approved_base,
            "NEXUS_APPROVED_ORIGIN": approved_origin,
            "NODE_PATH": str((runtime.root / "node_modules").resolve()),
        },
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        receipt = None
    tests = receipt.get("tests", []) if isinstance(receipt, dict) else []
    requests = receipt.get("requests", []) if isinstance(receipt, dict) else []
    tls = receipt.get("tls", []) if isinstance(receipt, dict) else []
    api = receipt.get("api", []) if isinstance(receipt, dict) else []
    assertion_steps = [
        one for one in (receipt.get("steps", []) if isinstance(receipt, dict) else [])
        if isinstance(one, dict) and one.get("category") == "expect"
    ]
    exact_request = any(
        isinstance(one, dict)
        and urlsplit(str(one.get("url", ""))).scheme.casefold() == "https"
        and f"https://{urlsplit(str(one.get('url', ''))).netloc.casefold()}" == approved_origin.casefold()
        for one in requests
    )
    receipt_ok = bool(
        isinstance(receipt, dict)
        and receipt.get("worker_mode") == "IN_PROCESS_WORKER_MAIN"
        and receipt.get("worker_ready") is True
        and receipt.get("worker_exited") is True
        and receipt.get("external_write_denied") is True
        and not denied_path.exists()
        and tests
        and all(
            isinstance(one, dict) and one.get("status") == one.get("expectedStatus")
            for one in tests
        )
        and assertion_steps
        and all(one.get("error") is None for one in assertion_steps)
        and exact_request and tls
        and all(
            str(one.get("protocol", "")).upper().startswith("TLS")
            and f"https://{urlsplit(str(one.get('url', ''))).netloc.casefold()}" == approved_origin.casefold()
            for one in tls if isinstance(one, dict)
        )
        and all(
            f"https://{urlsplit(str(one.get('url', ''))).netloc.casefold()}" == approved_origin.casefold()
            for one in api if isinstance(one, dict)
        )
    )
    return {
        "schema_version": 1,
        "passed": bool(broker.get("passed") and receipt_ok),
        "execution_mode": "unmodified-suite-inprocess-worker-main",
        "approved_base_url": approved_base,
        "approved_origin": approved_origin,
        "cli_args": args,
        "receipt": receipt,
        "receipt_attested": receipt_ok,
        "broker": broker,
    }
def _windows_environment() -> dict[str, str]:
    return {
        key: os.environ[key] for key in (
            "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
            "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
            "APPDATA", "USERNAME", "ALLUSERSPROFILE", "ProgramData",
            "PUBLIC", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
            "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles",
        ) if key in os.environ
    }


def _candidate_roots() -> list[Path]:
    requested = os.environ.get("NEXUS_PLAYWRIGHT_RUNTIME", "").strip()
    if requested:
        return [Path(requested).resolve()]
    source_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(sys.executable).resolve().parent / "playwright",
        source_root / "desktop" / "runtime" / "playwright",
    ]
    try:
        candidates.append(Path(__file__).resolve().parents[3] / "runtime" / "playwright")
    except IndexError:
        pass
    unique: list[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def discover_bundled_playwright_runtime(*, required: bool = False) -> BundledPlaywrightRuntime | None:
    """Find the immutable source/build or installed Playwright runtime.

    The returned paths can be granted read/execute authority as one root to a
    Windows AppContainer. Project-local node_modules and a system Node are not
    candidates, so verification does not silently weaken on a clean machine.
    """

    failures: list[str] = []
    for root in _candidate_roots():
        manifest_path = root.parent / "NEXUS_RUNTIME.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            metadata = manifest["playwright"]
            if metadata.get("schema_version") != 1:
                raise ValueError("unsupported manifest schema")
            values = {
                "node": root / metadata["node"],
                "cli": root / metadata["playwright_cli"],
                "playwright_module": root / metadata["playwright_module"],
                "test_module": root / metadata["playwright_test_module"],
                "browsers": root / metadata["browsers_path"],
                "chromium": root / metadata["chromium_executable"],
            }
            missing = [name for name, path in values.items() if not path.exists()]
            if missing:
                raise ValueError("missing " + ", ".join(missing))
            return BundledPlaywrightRuntime(
                root=root,
                **values,
                node_version=str(metadata["node_version"]),
                playwright_version=str(metadata["playwright_version"]),
                chromium_revision=str(metadata["chromium_revision"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"{root}: {error}")
    if required:
        detail = "; ".join(failures) or "no runtime candidates"
        raise RuntimeError("Bundled Playwright runtime is unavailable: " + detail)
    return None


def run_brokered_playwright_appcontainer(
    snapshot: Path,
    runner_script: Path,
    *,
    runner_args: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
    timeout: float = 45.0,
    runtime: BundledPlaywrightRuntime | None = None,
    approved_base_url: str | None = None,
    runner_nested_workspace: bool = False,
) -> dict[str, Any]:
    """Run one engine-owned Playwright script without AppContainer child spawn.

    Chromium and Node are independently launched by the trusted host with the
    same AppContainer profile. Chromium publishes a private CDP endpoint; the
    contained Node runner connects to it. The host never connects to CDP.
    A second engine-owned Node turn sends ``Browser.close`` after the runner,
    including when the runner fails, and the browser Job remains the bounded
    final teardown authority.
    """

    from .windows_containment import (
        appcontainer_available,
        run_appcontainer,
        verification_runtime_profile,
    )

    if os.name != "nt" or not appcontainer_available():
        raise OSError("Windows AppContainer is required for brokered Playwright")
    runtime = runtime or discover_bundled_playwright_runtime(required=True)
    assert runtime is not None
    snapshot = snapshot.resolve()
    runner_script = runner_script.resolve()
    if not snapshot.is_dir() or not runner_script.is_file() or not runner_script.is_relative_to(snapshot):
        raise ValueError("The engine-owned Playwright runner must be a file inside its snapshot")
    if timeout <= 0:
        raise ValueError("Playwright broker timeout must be positive")

    control = snapshot / ".nexus-verification"
    browser_snapshot = control / "playwright-browser"
    browser_snapshot.mkdir(parents=True, exist_ok=True)
    if browser_snapshot.is_symlink():
        raise ValueError("The Playwright browser authority may not be a link")
    closer = control / "playwright-close.cjs"
    closer.write_text(r'''
const path = require('node:path');
const { chromium } = require(path.join(process.env.NEXUS_BUNDLED_PLAYWRIGHT_ROOT, 'node_modules', 'playwright'));
(async () => {
  const browser = await chromium.connectOverCDP(process.env.NEXUS_CDP_ENDPOINT);
  const context = browser.contexts()[0];
  if (!context) throw new Error('brokered Chromium has no default context');
  const page = context.pages()[0] || await context.newPage();
  const session = await context.newCDPSession(page);
  await session.send('Browser.close');
})().catch(error => { console.error(error); process.exit(1); });
''', encoding="utf-8")

    profile = verification_runtime_profile()
    approved: tuple[str, str, str, int] | None = None
    proxy_snapshot: Path | None = None
    proxy_stop_token = ""
    proxy_closer_payload: dict[str, Any] | None = None
    proxy_routes: Path | None = None
    proxy_thread: threading.Thread | None = None
    proxy_results: queue.Queue[object] = queue.Queue()
    proxy_address = ""
    proxy_ready = approved_base_url is None
    if approved_base_url is not None:
        approved = normalize_approved_https_base_url(approved_base_url)
        _base_url, configured_origin, approved_host, approved_port = approved
        proxy_snapshot = control / "playwright-origin-proxy"
        proxy_snapshot.mkdir(parents=True, exist_ok=True)
        if proxy_snapshot.is_symlink():
            raise ValueError("The exact-origin proxy authority may not be a link")
        proxy_stop_token = secrets.token_hex(32)
        proxy_host = "127.0.0.1"
        with socket.socket() as proxy_reservation:
            proxy_reservation.bind((proxy_host, 0))
            proxy_port = int(proxy_reservation.getsockname()[1])
        proxy_address = f"http://{proxy_host}:{proxy_port}"
        proxy_environment = {
            **_windows_environment(),
            "HOME": str(proxy_snapshot), "USERPROFILE": str(proxy_snapshot),
            "TEMP": str(proxy_snapshot), "TMP": str(proxy_snapshot),
        }
        # Capabilities are granted to each process, not to the profile.  The
        # proxy gets Internet Client; Chromium and the runner share only its
        # profile's loopback endpoint and never receive general-network power.
        proxy_profile = profile
        proxy_timeout = timeout + 25.0

        def run_proxy() -> None:
            try:
                proxy_results.put(run_appcontainer(
                    proxy_snapshot,
                    [str(runtime.node), "-e", _EXACT_ORIGIN_PROXY_SOURCE,
                     approved_host, str(approved_port), proxy_host, str(proxy_port),
                     proxy_stop_token, configured_origin],
                    proxy_environment, proxy_timeout,
                    persistent_profile=proxy_profile,
                    read_execute_roots=(runtime.root,),
                    capability_sids=(INTERNET_CLIENT, PRIVATE_NETWORK_CLIENT_SERVER),
                ))
            except BaseException as error:
                proxy_results.put(error)

        proxy_thread = threading.Thread(
            target=run_proxy, name="nexus-exact-origin-proxy", daemon=True,
        )
        proxy_thread.start()
        proxy_stdout = proxy_snapshot / ".nexus-verification" / "contained-stdout.txt"
        proxy_deadline = time.monotonic() + min(20.0, timeout)
        while time.monotonic() < proxy_deadline:
            if not proxy_results.empty():
                break
            try:
                proxy_ready = "NEXUS_EXACT_ORIGIN_PROXY_READY" in proxy_stdout.read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                proxy_ready = False
            if proxy_ready:
                break
            time.sleep(0.05)

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    endpoint = f"http://127.0.0.1:{port}"
    capability = (PRIVATE_NETWORK_CLIENT_SERVER,)
    browser_results: queue.Queue[object] = queue.Queue()
    browser_environment = {
        **_windows_environment(),
        "HOME": str(browser_snapshot), "USERPROFILE": str(browser_snapshot),
        "TEMP": str(browser_snapshot), "TMP": str(browser_snapshot),
    }
    browser_argv = [
        str(runtime.chromium),
        "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
        "--disable-background-networking", "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        "--user-data-dir=" + str(browser_snapshot / "profile"), "about:blank",
    ]
    if proxy_address:
        browser_argv[1:1] = [
            "--proxy-server=" + proxy_address,
            "--proxy-bypass-list=<-loopback>",
            "--disable-quic",
        ]
    browser_timeout = timeout + 20.0

    def run_browser() -> None:
        try:
            browser_results.put(run_appcontainer(
                browser_snapshot, browser_argv, browser_environment, browser_timeout,
                persistent_profile=profile,
                read_execute_roots=(runtime.root,),
                capability_sids=capability,
            ))
        except BaseException as error:
            browser_results.put(error)

    browser_thread = threading.Thread(
        target=run_browser, name="nexus-contained-chromium", daemon=True,
    )
    if proxy_ready:
        browser_thread.start()
    browser_stderr = browser_snapshot / ".nexus-verification" / "contained-stderr.txt"
    ready = False
    readiness_deadline = time.monotonic() + min(20.0, timeout)
    while proxy_ready and time.monotonic() < readiness_deadline:
        if not browser_results.empty():
            break
        try:
            ready = "DevTools listening on" in browser_stderr.read_text(
                encoding="utf-8", errors="replace",
            )
        except OSError:
            ready = False
        if ready:
            break
        time.sleep(0.05)

    runner_payload: dict[str, Any] | None = None
    closer_payload: dict[str, Any] | None = None
    broker_error = ""
    if ready:
        common_environment = runtime.environment({
            **_windows_environment(),
            **dict(environment or {}),
            "NEXUS_CDP_ENDPOINT": endpoint,
        })
        if proxy_address:
            common_environment["NEXUS_ORIGIN_PROXY"] = proxy_address
        common_environment["NEXUS_ALLOWED_LOOPBACK_PORTS"] = ",".join(
            map(str, ([port, proxy_port] if proxy_address else [port]))
        )
        if approved is not None:
            common_environment["NEXUS_APPROVED_BASE_URL"] = approved[0]
            common_environment["NEXUS_APPROVED_ORIGIN"] = approved[1]
        try:
            runner_payload = run_appcontainer(
                snapshot,
                [str(runtime.node), "--permission", "--allow-fs-read=*", "--allow-fs-write=*",
                 str(runner_script), *map(str, runner_args)],
                common_environment, timeout,
                persistent_profile=profile,
                read_execute_roots=(runtime.root,),
                capability_sids=capability,
                map_authorized_roots=True,
                nested_mapped_cwd=runner_nested_workspace,
            )
        except BaseException as error:
            broker_error = "Contained Playwright runner failed to launch: " + str(error)
        # Browser.close is engine-owned and independent of project test code.
        if browser_thread.is_alive():
            try:
                closer_payload = run_appcontainer(
                    snapshot,
                    [str(runtime.node), "--permission", "--allow-fs-read=*", "--allow-fs-write=*", str(closer)],
                    common_environment, min(15.0, timeout),
                    persistent_profile=profile,
                    read_execute_roots=(runtime.root,),
                    capability_sids=capability,
                    map_authorized_roots=True,
                )
            except BaseException as error:
                broker_error = broker_error or "Contained Chromium closer failed to launch: " + str(error)
    else:
        broker_error = (
            "Exact-origin proxy did not produce its readiness receipt"
            if not proxy_ready else
            "Contained Chromium did not produce its CDP readiness receipt"
        )

    # Browser.close normally ends this immediately. If it did not, the
    # browser AppContainer Job reaches its own timeout and is terminated before
    # this API returns, so no broker process survives a failed runner/closer.
    if browser_thread.ident is not None:
        browser_thread.join(timeout=browser_timeout + 10.0)
    if browser_thread.is_alive():
        broker_error = broker_error or "Contained Chromium teardown did not complete"
        browser_payload: object = RuntimeError(broker_error)
    elif browser_results.empty():
        browser_payload = RuntimeError("Contained Chromium produced no process result")
    else:
        browser_payload = browser_results.get_nowait()
    if isinstance(browser_payload, BaseException):
        broker_error = broker_error or str(browser_payload)

    if proxy_thread is not None and proxy_thread.is_alive():
        proxy_control = control / "playwright-origin-proxy-control"
        proxy_control.mkdir(parents=True, exist_ok=True)
        try:
            proxy_closer_payload = run_appcontainer(
                proxy_control,
                [str(runtime.node), "-e", _EXACT_ORIGIN_PROXY_CLOSE_SOURCE,
                 "127.0.0.1", str(proxy_port), proxy_stop_token],
                {
                    **_windows_environment(),
                    "HOME": str(proxy_control), "USERPROFILE": str(proxy_control),
                    "TEMP": str(proxy_control), "TMP": str(proxy_control),
                },
                min(10.0, timeout), persistent_profile=profile,
                read_execute_roots=(runtime.root,),
                capability_sids=(PRIVATE_NETWORK_CLIENT_SERVER,),
            )
        except BaseException as error:
            broker_error = broker_error or "Exact-origin proxy closer failed: " + str(error)
    if proxy_thread is not None:
        proxy_thread.join(timeout=timeout + 30.0)
    if proxy_thread is None:
        proxy_payload: object | None = None
    elif proxy_thread.is_alive():
        proxy_payload = RuntimeError("Exact-origin proxy teardown did not complete")
        broker_error = broker_error or str(proxy_payload)
    elif proxy_results.empty():
        proxy_payload = RuntimeError("Exact-origin proxy produced no process result")
        broker_error = broker_error or str(proxy_payload)
    else:
        proxy_payload = proxy_results.get_nowait()
        if isinstance(proxy_payload, BaseException):
            broker_error = broker_error or str(proxy_payload)
    route_receipts: list[dict[str, Any]] = []
    if isinstance(proxy_payload, dict):
        for line in str(proxy_payload.get("stdout", "")).splitlines():
            if not line.startswith("NEXUS_ORIGIN_ROUTE "):
                continue
            try:
                value = json.loads(line.removeprefix("NEXUS_ORIGIN_ROUTE "))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                route_receipts.append(value)

    browser_ok = isinstance(browser_payload, dict) and browser_payload.get("exit_code") == 0
    runner_ok = isinstance(runner_payload, dict) and runner_payload.get("exit_code") == 0
    closer_ok = closer_payload is None or closer_payload.get("exit_code") == 0
    proxy_ok = (
        proxy_payload is None
        or isinstance(proxy_payload, dict) and proxy_payload.get("exit_code") == 0
    )
    proxy_closer_ok = (
        proxy_closer_payload is None
        or proxy_closer_payload.get("exit_code") == 0
    )
    exact_route_ok = (
    approved is None
    or any(
            item.get("route") == "https-connect"
            and item.get("allowed") is True
            and item.get("authority", "").casefold()
            == (
                f"[{approved[2]}]:{approved[3]}"
                if ":" in approved[2] else f"{approved[2]}:{approved[3]}"
            ).casefold()
            for item in route_receipts
        )
    )
    contained_processes = [
        one for one in (
            browser_payload, runner_payload, closer_payload, proxy_payload,
            proxy_closer_payload,
        ) if isinstance(one, dict)
    ]
    containment_sids = {
        str(one.get("containment_sid") or "") for one in contained_processes
    }
    boundary_inheritance_attested = bool(
        contained_processes and "" not in containment_sids and len(containment_sids) == 1
        and all(one.get("containment_profile") == "windows-appcontainer-job-v1" for one in contained_processes)
    )
    return {
        "schema_version": 1,
        "passed": bool(
            ready and proxy_ready and browser_ok and runner_ok and closer_ok
            and proxy_ok and proxy_closer_ok and exact_route_ok
            and boundary_inheritance_attested and not broker_error
        ),
        "endpoint_scope": "same-profile-appcontainer-loopback",
        "profile": profile,
        "capability_sids": list(capability),
        "external_write_authority": str(snapshot),
        "immutable_runtime_authority": str(runtime.root),
        "readiness_attested": ready,
        "boundary_inheritance_attested": boundary_inheritance_attested,
        "runner": runner_payload,
        "closer": closer_payload,
        "browser": browser_payload,
        "proxy": proxy_payload,
        "proxy_closer": proxy_closer_payload,
        "proxy_address": proxy_address,
        "approved_base_url": approved[0] if approved else None,
        "approved_origin": approved[1] if approved else None,
        "origin_routes": route_receipts,
        "exact_origin_route_attested": exact_route_ok,
        "route_mode": "REMOTE_TLS_TUNNEL" if approved is not None else None,
        "process_capabilities": {
            "origin_proxy": [INTERNET_CLIENT, PRIVATE_NETWORK_CLIENT_SERVER]
            if approved is not None else [],
            "browser": list(capability),
            "runner": list(capability),
            "browser_closer": list(capability),
            "proxy_closer": list(capability) if approved is not None else [],
        },
        "error": broker_error,
    }
