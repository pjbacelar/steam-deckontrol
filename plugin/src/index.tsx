import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  TextField,
  DropdownItem,
  Field,
  Spinner,
  staticClasses,
  definePlugin,
} from "@decky/ui";
import { callable, toaster } from "@decky/api";
import { useEffect, useState, useCallback } from "react";
import { FaGamepad } from "react-icons/fa";

interface Host {
  name: string;
  ip: string;
  port: number;
  busy: boolean;
  version: number;
}

interface ConnectResult {
  ok: boolean;
  name?: string;
  error?: string;
}

interface Status {
  connected: boolean;
  host: string;
  fps: number;
  frames: number;
  capturing: boolean;
  device: string;
  input_only: boolean;
}

// Backend callables (see main.py Plugin methods).
const discoverHosts = callable<[], Host[]>("discover_hosts");
const connect = callable<[string, number, string], ConnectResult>("connect");
const disconnect = callable<[], void>("disconnect");
const getStatus = callable<[], Status>("get_status");
const setInputOnlyMode = callable<[boolean], void>("set_input_only_mode");

const ERROR_LABEL: Record<string, string> = {
  no_controller: "No controller found on the Deck.",
  bad_token: "Pairing token rejected by the host.",
  no_host: "Host did not respond — check IP/port.",
};

function Content() {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [selectedIp, setSelectedIp] = useState<string>("");
  const [token, setToken] = useState<string>("");
  const [scanning, setScanning] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await getStatus());
    } catch {
      /* backend not ready yet */
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    const id = setInterval(refreshStatus, 1000);
    return () => clearInterval(id);
  }, [refreshStatus]);

  const scan = async () => {
    setScanning(true);
    try {
      const found = await discoverHosts();
      setHosts(found);
      if (found.length && !selectedIp) {
        setSelectedIp(found[0].ip);
      }
      if (!found.length) {
        toaster.toast({ title: "Deckontrol", body: "No hosts found on the LAN." });
      }
    } finally {
      setScanning(false);
    }
  };

  const doConnect = async () => {
    const host = hosts.find((h) => h.ip === selectedIp);
    const ip = selectedIp.trim();
    if (!ip) {
      toaster.toast({ title: "Deckontrol", body: "Pick a host or enter an IP." });
      return;
    }
    setConnecting(true);
    try {
      const result = await connect(ip, host?.port ?? 27970, token.trim());
      if (result.ok) {
        toaster.toast({ title: "Deckontrol", body: `Connected to ${result.name}` });
      } else {
        toaster.toast({ title: "Deckontrol", body: ERROR_LABEL[result.error ?? ""] ?? "Connection failed." });
      }
      await refreshStatus();
    } finally {
      setConnecting(false);
    }
  };

  const doDisconnect = async () => {
    await disconnect();
    await refreshStatus();
  };

  const connected = status?.connected ?? false;

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field label="Link">{connected ? `Connected → ${status?.host}` : "Disconnected"}</Field>
        </PanelSectionRow>
        {connected && (
          <>
            <PanelSectionRow>
              <Field label="Rate">{`${status?.fps ?? 0} Hz · ${status?.frames ?? 0} frames`}</Field>
            </PanelSectionRow>
            <PanelSectionRow>
              <Field label="Capturing">{status?.capturing ? status?.device : "—"}</Field>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      {!connected && (
        <PanelSection title="Connect">
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={scan} disabled={scanning}>
              {scanning ? <Spinner width={16} height={16} /> : "Scan LAN for hosts"}
            </ButtonItem>
          </PanelSectionRow>
          {hosts.length > 0 && (
            <PanelSectionRow>
              <DropdownItem
                label="Host"
                rgOptions={hosts.map((h) => ({
                  label: `${h.name} (${h.ip})${h.busy ? " · busy" : ""}`,
                  data: h.ip,
                }))}
                selectedOption={selectedIp}
                onChange={(o) => setSelectedIp(o.data as string)}
              />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <TextField
              label="Host IP (manual)"
              value={selectedIp}
              onChange={(e) => setSelectedIp(e.target.value)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <TextField
              label="Pairing token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={doConnect} disabled={connecting}>
              {connecting ? <Spinner width={16} height={16} /> : "Connect"}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {connected && (
        <PanelSection title="Mode">
          <PanelSectionRow>
            <ToggleField
              label="Input-only mode"
              description="Forward input only; suppress the Remote Play display surface."
              checked={status?.input_only ?? false}
              onChange={(v) => setInputOnlyMode(v).then(refreshStatus)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={doDisconnect}>
              Disconnect
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}

export default definePlugin(() => ({
  name: "Steam Deckontrol",
  titleView: <div className={staticClasses.Title}>Deckontrol</div>,
  content: <Content />,
  icon: <FaGamepad />,
  onDismount() {
    // Leave the stream running if the panel is closed; teardown happens on
    // explicit Disconnect or plugin unload.
  },
}));
