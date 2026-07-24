import { describe, expect, it } from "vitest";
import { checkBasicAuth } from "../src/web/auth.js";

function basicHeader(user: string, pass: string): string {
  return "Basic " + Buffer.from(`${user}:${pass}`).toString("base64");
}

describe("checkBasicAuth", () => {
  it("accepts the correct username/password", () => {
    expect(checkBasicAuth(basicHeader("alice", "hunter2"), "alice", "hunter2")).toBe(true);
  });

  it("rejects a wrong password", () => {
    expect(checkBasicAuth(basicHeader("alice", "wrong"), "alice", "hunter2")).toBe(false);
  });

  it("rejects a wrong username", () => {
    expect(checkBasicAuth(basicHeader("bob", "hunter2"), "alice", "hunter2")).toBe(false);
  });

  it("rejects a missing Authorization header", () => {
    expect(checkBasicAuth(undefined, "alice", "hunter2")).toBe(false);
  });

  it("rejects a non-Basic Authorization header", () => {
    expect(checkBasicAuth("Bearer sometoken", "alice", "hunter2")).toBe(false);
  });

  it("rejects malformed base64 without a colon separator", () => {
    const noColon = "Basic " + Buffer.from("nopasswordhere").toString("base64");
    expect(checkBasicAuth(noColon, "alice", "hunter2")).toBe(false);
  });

  it("handles credentials of different lengths without throwing", () => {
    expect(checkBasicAuth(basicHeader("a", "b"), "alice", "hunter2")).toBe(false);
  });
});
