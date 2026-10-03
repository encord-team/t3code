import { describe, it, assert } from "@effect/vitest";
import { DEFAULT_CLIENT_SETTINGS } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Layer from "effect/Layer";
import * as DesktopClientSettings from "../../settings/DesktopClientSettings.ts";
import * as DesktopSnapShot from "../../snapShot/DesktopSnapShot.ts";
import * as DesktopWindow from "../../window/DesktopWindow.ts";
import { setClientSettings } from "./clientSettings.ts";

describe("desktop borderless fullscreen preference", () => {
  it.effect("applies entry and exit after the preference is saved", () => {
    let saved = DEFAULT_CLIENT_SETTINGS;
    let active = false;
    const observedPreferences: boolean[] = [];
    const layer = Layer.mergeAll(
      Layer.mock(DesktopClientSettings.DesktopClientSettings)({
        get: Effect.sync(() => Option.some(saved)),
        set: (settings) =>
          Effect.sync(() => {
            saved = settings;
          }),
      }),
      Layer.mock(DesktopWindow.DesktopWindow)({
        setBorderlessFullscreen: (enabled) =>
          Effect.sync(() => {
            observedPreferences.push(saved.borderlessFullscreen);
            active = enabled;
          }),
      }),
      Layer.mock(DesktopSnapShot.DesktopSnapShot)({ configure: () => Effect.void }),
    );
    return Effect.gen(function* () {
      yield* setClientSettings.handler({ ...saved, borderlessFullscreen: true });
      assert.isTrue(active);
      yield* setClientSettings.handler({ ...saved, appearanceContrast: 110 });
      assert.deepEqual(observedPreferences, [true]);
      yield* setClientSettings.handler({ ...saved, borderlessFullscreen: false });
      assert.isFalse(active);
      assert.deepEqual(observedPreferences, [true, false]);
    }).pipe(Effect.provide(layer));
  });
});
