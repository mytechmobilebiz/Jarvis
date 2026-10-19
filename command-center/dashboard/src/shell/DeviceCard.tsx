// The "Intern 2" card in the sidebar. No Intern 2 integration exists, so it can only say "Not connected" (owner request 6).
import { Icon } from "../icons";

export type DeviceState = "not-connected" | "connected" | "disconnected";

export interface DeviceStatus {
  state: DeviceState;
  /** The words shown next to the dot. */
  label: string;
  /** Why, for screen readers and the tooltip. */
  detail: string;
}

/** The only source of Intern 2's state. Until a verified heartbeat exists (device pairing with its own token), it returns
 * "not connected", so no code path can show "Connected" without one. */
export function deviceStatus(): DeviceStatus {
  return {
    state: "not-connected",
    label: "Not connected",
    detail: "Intern 2 is not connected: the Command Center has no Intern 2 integration yet. It will show Connected only after a verified heartbeat.",
  };
}

export default function DeviceCard() {
  const status = deviceStatus();
  return (
    <div className={`device-card device-${status.state}`} title={status.detail}>
      {/* A neutral glyph, not a product photo: a photo would suggest a paired device. */}
      <span className="device-glyph" aria-hidden>
        <Icon name="device" size={26} strokeWidth={1.5} />
      </span>
      <div className="device-text">
        <span className="device-name nav-label">Intern 2</span>
        <span className="device-state nav-label">
          <span className="dot dot-muted" aria-hidden /> {status.label}
        </span>
        <span className="sr-only">{status.detail}</span>
      </div>
    </div>
  );
}
