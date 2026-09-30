import type { BrowserWindow } from "electron";
import { WINDOW_FULLSCREEN_STATE_CHANNEL } from "../ipc/channels.ts";

type FullscreenWindow = Pick<
  BrowserWindow,
  | "isDestroyed"
  | "isFullScreen"
  | "isSimpleFullScreen"
  | "isMaximized"
  | "getBounds"
  | "setBounds"
  | "setFullScreen"
  | "setSimpleFullScreen"
> & {
  once: (event: "leave-full-screen", listener: () => void) => unknown;
  removeListener: (event: "leave-full-screen", listener: () => void) => unknown;
  webContents: { send: (channel: string, fullscreen: boolean) => void };
};

const pendingEntries = new WeakMap<FullscreenWindow, () => void>();

/** Simple fullscreen has no native fullscreen events, so publish its layout state here. */
export function setBorderlessFullscreen(window: FullscreenWindow, enabled: boolean): void {
  const pending = pendingEntries.get(window);
  if (pending) {
    window.removeListener("leave-full-screen", pending);
    pendingEntries.delete(window);
  }
  if (window.isDestroyed()) return;
  if (enabled && window.isFullScreen()) {
    const enter = () => {
      pendingEntries.delete(window);
      setBorderlessFullscreen(window, true);
    };
    pendingEntries.set(window, enter);
    window.once("leave-full-screen", enter);
    window.setFullScreen(false);
    return;
  }
  if (window.isSimpleFullScreen() === enabled) return;
  if (enabled && !window.isMaximized()) {
    // Electron 44 does not snapshot manual moves/resizes on simple fullscreen entry.
    // Setting the current bounds refreshes the frame it restores on exit.
    window.setBounds(window.getBounds(), false);
  }
  window.setSimpleFullScreen(enabled);
  window.webContents.send(WINDOW_FULLSCREEN_STATE_CHANNEL, enabled);
}
