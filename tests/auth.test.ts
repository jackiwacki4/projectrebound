import { describe, expect, it } from "vitest";
import { constants, generateKeyPairSync, createVerify } from "node:crypto";
import { signRequest } from "../src/kalshi/auth.js";

describe("signRequest", () => {
  it("produces a signature verifiable with the matching public key (RSA-PSS/SHA-256)", () => {
    const { privateKey, publicKey } = generateKeyPairSync("rsa", {
      modulusLength: 2048,
      privateKeyEncoding: { type: "pkcs1", format: "pem" },
      publicKeyEncoding: { type: "spki", format: "pem" },
    });

    const timestampMs = "1753267200000";
    const method = "GET";
    const path = "/trade-api/v2/portfolio/balance";

    const signature = signRequest(privateKey, timestampMs, method, path);

    const verifier = createVerify("RSA-SHA256");
    verifier.update(`${timestampMs}${method}${path}`);
    verifier.end();
    const isValid = verifier.verify(
      {
        key: publicKey,
        padding: constants.RSA_PKCS1_PSS_PADDING,
        saltLength: constants.RSA_PSS_SALTLEN_DIGEST,
      },
      signature,
      "base64"
    );

    expect(isValid).toBe(true);
  });

  it("strips the query string before signing", () => {
    const { privateKey, publicKey } = generateKeyPairSync("rsa", {
      modulusLength: 2048,
      privateKeyEncoding: { type: "pkcs1", format: "pem" },
      publicKeyEncoding: { type: "spki", format: "pem" },
    });

    const signature = signRequest(privateKey, "1000", "GET", "/markets?limit=5");

    const verifier = createVerify("RSA-SHA256");
    verifier.update("1000GET/markets"); // no query string
    verifier.end();
    const isValid = verifier.verify(
      {
        key: publicKey,
        padding: constants.RSA_PKCS1_PSS_PADDING,
        saltLength: constants.RSA_PSS_SALTLEN_DIGEST,
      },
      signature,
      "base64"
    );

    expect(isValid).toBe(true);
  });
});
