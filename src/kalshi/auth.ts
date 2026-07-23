import { constants, createSign } from "node:crypto";

/**
 * Kalshi request signing: RSA-PSS(SHA-256) over `${timestampMs}${method}${path}`,
 * where path excludes the query string. Verified against multiple independent
 * current sources at build time (2026-07) -- see README "Verify before going live".
 */
export function signRequest(
  privateKeyPem: string,
  timestampMs: string,
  method: string,
  path: string
): string {
  const pathWithoutQuery = path.split("?")[0];
  const message = `${timestampMs}${method}${pathWithoutQuery}`;
  const signer = createSign("RSA-SHA256");
  signer.update(message);
  signer.end();
  return signer.sign(
    {
      key: privateKeyPem,
      padding: constants.RSA_PKCS1_PSS_PADDING,
      saltLength: constants.RSA_PSS_SALTLEN_DIGEST,
    },
    "base64"
  );
}

export interface AuthHeaders {
  "KALSHI-ACCESS-KEY": string;
  "KALSHI-ACCESS-TIMESTAMP": string;
  "KALSHI-ACCESS-SIGNATURE": string;
  [header: string]: string;
}

export function buildAuthHeaders(
  apiKeyId: string,
  privateKeyPem: string,
  method: string,
  path: string
): AuthHeaders {
  const timestampMs = Date.now().toString();
  return {
    "KALSHI-ACCESS-KEY": apiKeyId,
    "KALSHI-ACCESS-TIMESTAMP": timestampMs,
    "KALSHI-ACCESS-SIGNATURE": signRequest(privateKeyPem, timestampMs, method, path),
  };
}
