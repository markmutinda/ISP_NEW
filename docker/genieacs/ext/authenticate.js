"use strict";
// GenieACS extension — called from the cwmp.auth expression to validate
// each CPE's Basic Auth credentials against Django's public credential
// store (Tr069DeviceIndex). Cached briefly since every Inform re-checks.
const http = require("http");

const DJANGO_HOST = process.env.NETILY_AUTH_HOST || "web";
const DJANGO_PORT = parseInt(process.env.NETILY_AUTH_PORT || "8000", 10);
const AUTH_TOKEN = process.env.NETILY_AUTH_TOKEN || "";

const cache = new Map();
const CACHE_MS = 10000;

function fetchCreds(serial, callback) {
  if (!serial) return callback(null, { username: "", password: "" });

  const cached = cache.get(serial);
  if (cached && Date.now() < cached.expires) return callback(null, cached.data);

  const req = http.request(
    {
      hostname: DJANGO_HOST,
      port: DJANGO_PORT,
      path: `/api/v1/tr069/webhook/credentials/?serial=${encodeURIComponent(serial)}`,
      method: "GET",
      headers: { "X-TR069-Webhook-Secret": AUTH_TOKEN },
      timeout: 5000,
    },
    (res) => {
      let raw = "";
      res.on("data", (c) => (raw += c));
      res.on("end", () => {
        if (res.statusCode !== 200) {
          return callback(null, { username: "", password: "" });
        }
        try {
          const data = JSON.parse(raw);
          const result = { username: data.username || "", password: data.password || "" };
          cache.set(serial, { data: result, expires: Date.now() + CACHE_MS });
          callback(null, result);
        } catch (e) {
          callback(null, { username: "", password: "" });
        }
      });
    }
  );
  req.on("error", () => callback(null, { username: "", password: "" }));
  req.on("timeout", () => {
    req.destroy();
    callback(null, { username: "", password: "" });
  });
  req.end();
}

exports.getUsername = function (args, callback) {
  fetchCreds(args[0], (err, data) => callback(null, data.username));
};

exports.getPassword = function (args, callback) {
  fetchCreds(args[0], (err, data) => callback(null, data.password));
};