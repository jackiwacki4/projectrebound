import { timingSafeEqual } from "node:crypto";

function safeEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) {
    // Compare something of equal length anyway so a mismatched length
    // doesn't return measurably faster than a mismatched value.
    timingSafeEqual(bufA, bufA);
    return false;
  }
  return timingSafeEqual(bufA, bufB);
}

export function checkBasicAuth(
  authorizationHeader: string | undefined,
  username: string,
  password: string
): boolean {
  if (!authorizationHeader?.startsWith("Basic ")) return false;
  const decoded = Buffer.from(authorizationHeader.slice(6), "base64").toString("utf8");
  const sepIndex = decoded.indexOf(":");
  if (sepIndex === -1) return false;
  const user = decoded.slice(0, sepIndex);
  const pass = decoded.slice(sepIndex + 1);
  return safeEqual(user, username) && safeEqual(pass, password);
}
