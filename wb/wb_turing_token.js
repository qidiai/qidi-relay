'use strict';
// WorkBuddy TuringShield device token fetcher (N-API, ABI stable)
const ADDON = "C:\\Users\\?\\AppData\\Local\\Programs\\WorkBuddy\\resources\\app.asar.unpacked\\native\\turing-sdk";
const CHANNEL_ID = 109144;
const PRODUCT_NAME = "WorkBuddy";
const PRODUCT_VERSION = "5.5.6";

function fail(err) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.message || err) }));
    process.exit(1);
}

let sdk;
try { sdk = require(ADDON); } catch (e) { fail("load: " + e); }
if (!sdk.isSupported()) fail("unsupported: " + sdk.getLoadError());

Promise.resolve()
    .then(() => sdk.configure(CHANNEL_ID, PRODUCT_NAME, PRODUCT_VERSION, false))
    .then(() => sdk.fetchDeviceToken({}))
    .then((result) => {
        process.stdout.write(JSON.stringify({ ok: true, result }));
        process.exit(0);
    })
    .catch((e) => fail("fetch: " + e));

setTimeout(() => fail("timeout after 30s"), 30000).unref();