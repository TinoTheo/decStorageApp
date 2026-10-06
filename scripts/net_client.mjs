#!/usr/bin/env node
/**
 * Command-line wrapper around the browser client, used by the network test
 * (scripts/network_e2e.py) to act as a real user: everything is encrypted and
 * decrypted here, exactly as in the browser.
 *
 *   node net_client.mjs register --user U --pass P
 *   node net_client.mjs upload   --user U --pass P --size BYTES --marker TEXT
 *   node net_client.mjs list     --user U --pass P
 *   node net_client.mjs download --user U --pass P --file ID
 *   node net_client.mjs delete   --user U --pass P --file ID
 *
 * BASE_URL picks the coordinator. Output is one line of JSON.
 */

import { createHash } from "node:crypto";

import { DStoreClient } from "../web/src/client.js";
import { randomBytes } from "../web/src/crypto.js";

const [command, ...rest] = process.argv.slice(2);
const args = {};
for (let i = 0; i < rest.length; i += 2) args[rest[i].replace(/^--/, "")] = rest[i + 1];

const client = new DStoreClient({ baseUrl: process.env.BASE_URL || "http://127.0.0.1:8000", retries: 3 });
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

async function main() {
  if (command === "register") {
    await client.register(args.user, args.pass);
    return { ok: true };
  }
  await client.login(args.user, args.pass);

  switch (command) {
    case "upload": {
      const size = Number(args.size);
      const bytes = randomBytes(size);
      const marker = new TextEncoder().encode(args.marker || "PLAINTEXT");
      for (let offset = 0; offset + marker.length < size; offset += 64 * 1024) bytes.set(marker, offset);
      const manifest = await client.uploadFile(new Blob([bytes]), { name: args.name || "field-report.pdf" });
      return { fileId: manifest.id, sha256: sha256(bytes), size, segments: manifest.segment_count };
    }
    case "list":
      return { files: await client.listFiles() };
    case "download": {
      const result = await client.downloadFile(args.file);
      const bytes = new Uint8Array(await result.blob.arrayBuffer());
      return { name: result.name, size: bytes.length, sha256: sha256(bytes) };
    }
    case "delete":
      await client.deleteFile(args.file);
      return { ok: true };
    default:
      throw new Error(`unknown command ${command}`);
  }
}

main()
  .then((result) => console.log(JSON.stringify(result)))
  .catch((err) => {
    console.log(JSON.stringify({ error: err.message, status: err.status ?? null }));
    process.exit(1);
  });
