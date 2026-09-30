import * as NodeEvents from "node:events";
import { describe, expect, it } from "vite-plus/test";
import { setBorderlessFullscreen } from "./DesktopFullscreen.ts";

function makeWindow() {
  const events = new NodeEvents.EventEmitter();
  const window = Object.assign(events, {
    destroyed: false,
    native: false,
    simple: false,
    bounds: { x: 100, y: 100, width: 1100, height: 780 },
    normalBounds: { x: 100, y: 100, width: 1100, height: 780 },
    isMaximized: () => false,
    getBounds: () => window.bounds,
    setBounds: (bounds: typeof window.bounds) => {
      window.bounds = bounds;
      window.normalBounds = bounds;
    },
    published: [] as boolean[],
    isDestroyed: () => window.destroyed,
    isFullScreen: () => window.native,
    isSimpleFullScreen: () => window.simple,
    setFullScreen: (_enabled: boolean) => {},
    setSimpleFullScreen: (enabled: boolean) => {
      window.simple = enabled;
      // Electron restores its saved frame on exit; it does not snapshot manual resizes on entry.
      window.bounds = enabled ? { x: 0, y: 0, width: 1920, height: 1080 } : window.normalBounds;
    },
    webContents: {
      send: (_channel: string, enabled: boolean) => {
        window.published.push(enabled);
      },
    },
  });
  return window;
}

describe("borderless fullscreen", () => {
  it("enters and leaves on the current desktop and publishes layout state", () => {
    const window = makeWindow();
    setBorderlessFullscreen(window, true);
    expect(window.simple).toBe(true);
    expect(window.native).toBe(false);
    setBorderlessFullscreen(window, false);
    expect(window.simple).toBe(false);
    expect(window.published).toEqual([true, false]);
  });
  it("restores the latest manually moved and resized bounds", () => {
    const window = makeWindow();
    const resized = { x: 250, y: 150, width: 1300, height: 900 };
    window.bounds = resized;
    setBorderlessFullscreen(window, true);
    expect(window.normalBounds).toEqual(resized);
    setBorderlessFullscreen(window, false);
    expect(window.bounds).toEqual(resized);
  });
  it("does not reapply an unchanged mode", () => {
    const window = makeWindow();
    setBorderlessFullscreen(window, true);
    setBorderlessFullscreen(window, true);
    expect(window.published).toEqual([true]);
  });
  it("waits for native fullscreen to leave", () => {
    const window = makeWindow();
    window.native = true;
    setBorderlessFullscreen(window, true);
    expect(window.simple).toBe(false);
    window.native = false;
    window.emit("leave-full-screen");
    expect(window.simple).toBe(true);
  });
  it("cancels entry when disabled during a native transition", () => {
    const window = makeWindow();
    window.native = true;
    setBorderlessFullscreen(window, true);
    setBorderlessFullscreen(window, false);
    window.native = false;
    window.emit("leave-full-screen");
    expect(window.simple).toBe(false);
    expect(window.published).toEqual([]);
  });
  it("ignores windows destroyed during a native transition", () => {
    const window = makeWindow();
    window.native = true;
    setBorderlessFullscreen(window, true);
    window.destroyed = true;
    window.native = false;
    window.emit("leave-full-screen");
    expect(window.simple).toBe(false);
  });
});
