"use strict";
const http = require("http");
const HOST = process.env.NETILY_AUTH_HOST || "web";
const PORT = parseInt(process.env.NETILY_AUTH_PORT || "8000", 10);
const TOKEN = process.env.NETILY_AUTH_TOKEN || "";

exports.informed = function (args, callback) {
  const body = JSON.stringify({ serial_number: args[0], genieacs_device_id: args[1] });
  const req = http.request(
    {
      hostname: HOST, port: PORT, method: "POST",
      path: "/api/v1/tr069/webhook/inform/", timeout: 5000,
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        "X-TR069-Webhook-Secret": TOKEN,
      },
    },
    (res) => { res.resume(); res.on("end", () => callback(null, res.statusCode)); }
  );
  req.on("error", () => callback(null, 0));
  req.on("timeout", () => { req.destroy(); callback(null, 0); });
  req.write(body);
  req.end();
};