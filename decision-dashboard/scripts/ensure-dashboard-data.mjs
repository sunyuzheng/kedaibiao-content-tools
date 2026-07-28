import { access, copyFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const localState = resolve(root, "app", "podcast-state.local.json");
const exampleState = resolve(root, "app", "podcast-state.example.json");

try {
  await access(localState);
} catch {
  await copyFile(exampleState, localState);
  console.log("Created local dashboard state from the public example.");
}
