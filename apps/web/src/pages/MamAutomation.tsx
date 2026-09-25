import { useState, type ReactNode } from "react";
import type { components } from "../api/schema";

export type AutomationSettings = components["schemas"]["AccountAutomation"];
export type UploadAmount = number | "max";

export const AUTOMATION_DEFAULTS: AutomationSettings = {
  seedbox_ip: false,
  seedbox_interval_seconds: 3600,
  auto_vip: false,
  vip_interval_hours: 24,
  use_wedge: false,
  wedge_min_size: false,
  wedge_min_size_mb: 0,
  protect_ratio: false,
  ratio_below: 2.5,
  ratio_buy_gb: 50,
  maintain_buffer: false,
  buffer_below_gb: 10,
  buffer_buy_gb: 50,
  spend_bonus: false,
  bonus_above: 5000,
  bonus_buy_gb: 50,
  upload_interval_hours: 3,
};

export function NumberSetting({
  label,
  value,
  onChange,
  min = 0,
  max = 100000,
  step = 1,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  // Keep the draft in the input so clearing and replacing a number never snaps
  // back to the previous value. Native validity gates every settings write.
  return (
    <label>
      {label}
      <input
        type="number"
        required
        min={min}
        max={max}
        step={step}
        defaultValue={Number.isFinite(value) ? value : ""}
        onChange={(event) =>
          onChange(
            event.target.validity.valid ? Number(event.target.value) : NaN,
          )
        }
      />
    </label>
  );
}

const amounts = [50, 100, 250, 500];
export function UploadSelect({
  label,
  value,
  onChange,
}: {
  label: string;
  value: UploadAmount;
  onChange: (value: UploadAmount) => void;
}) {
  const [custom, setCustom] = useState(
    value !== "max" && !amounts.includes(value),
  );
  return (
    <div className="mam-amount-picker">
      <label>
        {label}
        <select
          value={custom ? "custom" : String(value)}
          onChange={(event) => {
            const next = event.target.value;
            setCustom(next === "custom");
            onChange(
              next === "custom" ? 50 : next === "max" ? "max" : Number(next),
            );
          }}
        >
          {amounts.map((amount) => (
            <option key={amount} value={amount}>
              {amount} GiB · {(amount * 500).toLocaleString()} points
            </option>
          ))}
          <option value="max">All I can afford · all available points</option>
          <option value="custom">Custom amount…</option>
        </select>
      </label>
      {custom && (
        <NumberSetting
          label={`${label} — custom GiB`}
          value={typeof value === "number" ? value : 50}
          min={50}
          onChange={onChange}
        />
      )}
    </div>
  );
}

function Rule({
  title,
  checked,
  onChange,
  description,
  children,
}: {
  title: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  description: string;
  children?: ReactNode;
}) {
  return (
    <section className="mam-rule" data-enabled={checked} aria-label={title}>
      <label className="check-label">
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        {title}
      </label>
      <p className="muted">{description}</p>
      {checked && <div className="automation-fields">{children}</div>}
    </section>
  );
}

export function AccountAutomation({
  value,
  onChange,
  checks,
}: {
  value: AutomationSettings;
  onChange: (value: AutomationSettings) => void;
  checks?: components["schemas"]["MAMAutomationChecks"];
}) {
  function set<K extends keyof AutomationSettings>(
    key: K,
    next: AutomationSettings[K],
  ) {
    if (typeof next === "number" && !Number.isFinite(next)) return;
    onChange({ ...value, [key]: next });
  }
  const count = [
    value.seedbox_ip,
    value.auto_vip,
    value.use_wedge,
    value.protect_ratio,
    value.maintain_buffer,
    value.spend_bonus,
  ].filter(Boolean).length;
  return (
    <details className="account-automation mam-automation">
      <summary>
        <span>Account automation</span>
        <span className="account-automation-state">
          {count ? `${count} enabled` : "All off"}
        </span>
      </summary>
      <p className="muted">
        Rules run after you save. Purchases spend the shared MAM account’s bonus
        points.
      </p>
      <dl className="mam-automation-checks">
        {(["upload", "vip", "seedbox"] as const).map((key) => (
          <div key={key}>
            <dt>
              {key === "upload"
                ? "Upload rules"
                : key === "vip"
                  ? "VIP top-up"
                  : "Seedbox IP"}
            </dt>
            <dd>
              {checks?.[key]
                ? `Last checked ${new Date(checks[key]!).toLocaleString()}`
                : "Not checked yet"}
            </dd>
          </div>
        ))}
      </dl>
      <div className="mam-rules">
        <Rule
          title="Protect minimum ratio"
          checked={value.protect_ratio}
          onChange={(next) => set("protect_ratio", next)}
          description="When your ratio drops below this threshold, buy upload credit once per check."
        >
          <NumberSetting
            label="If ratio falls below"
            value={value.ratio_below}
            min={0.01}
            max={1000}
            step={0.01}
            onChange={(next) => set("ratio_below", next)}
          />
          <UploadSelect
            label="Ratio rule purchase"
            value={value.ratio_buy_gb}
            onChange={(next) => set("ratio_buy_gb", next)}
          />
        </Rule>
        <Rule
          title="Maintain credit reserve"
          checked={value.maintain_buffer}
          onChange={(next) => set("maintain_buffer", next)}
          description="Reserve is uploaded minus downloaded. Buys once per check, unless the ratio rule already bought credit."
        >
          <NumberSetting
            label="If reserve falls below (GiB)"
            value={value.buffer_below_gb}
            max={10000000}
            step={0.1}
            onChange={(next) => set("buffer_below_gb", next)}
          />
          <UploadSelect
            label="Reserve rule purchase"
            value={value.buffer_buy_gb}
            onChange={(next) => set("buffer_buy_gb", next)}
          />
        </Rule>
        <Rule
          title="Spend excess bonus points"
          checked={value.spend_bonus}
          onChange={(next) => set("spend_bonus", next)}
          description="Buys while points exceed the threshold, up to 12 purchases per check. A purchase can take the balance below the threshold; this is not a points reserve."
        >
          <NumberSetting
            label="If bonus points exceed"
            value={value.bonus_above}
            max={100000000}
            onChange={(next) => set("bonus_above", next)}
          />
          <UploadSelect
            label="Bonus rule purchase"
            value={value.bonus_buy_gb}
            onChange={(next) => set("bonus_buy_gb", next)}
          />
        </Rule>
        {(value.protect_ratio ||
          value.maintain_buffer ||
          value.spend_bonus) && (
          <div className="mam-rule-note">
            <NumberSetting
              label="Upload check interval (hours)"
              value={value.upload_interval_hours}
              min={1}
              max={168}
              onChange={(next) => set("upload_interval_hours", next)}
            />
            <p className="muted">
              The API requires at least 50 GiB per purchase (25,000 points).
              “All I can afford” can spend the entire balance.
            </p>
          </div>
        )}
        <Rule
          title="Auto-max VIP"
          checked={value.auto_vip}
          onChange={(next) => set("auto_vip", next)}
          description="Top up toward MAM’s 90-day limit. The API supports max only, with a minimum seven-day purchase. MAM checks rank and eligibility."
        >
          <NumberSetting
            label="Top-up interval (hours)"
            value={value.vip_interval_hours}
            min={1}
            max={168}
            onChange={(next) => set("vip_interval_hours", next)}
          />
        </Rule>
        <Rule
          title="Use a Freeleech wedge on download"
          checked={value.use_wedge}
          onChange={(next) => set("use_wedge", next)}
          description="Spends a wedge you already own. Skips known freeleech and active VIP freeleech. This does not buy wedges; MAM does not refund wedges spent on VIP torrents."
        >
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.wedge_min_size}
              onChange={(event) => set("wedge_min_size", event.target.checked)}
            />
            Only for torrents larger than a minimum size
          </label>
          {value.wedge_min_size && (
            <NumberSetting
              label="Minimum size (MB)"
              value={value.wedge_min_size_mb}
              max={10000000}
              step={0.1}
              onChange={(next) => set("wedge_min_size_mb", next)}
            />
          )}
        </Rule>
        <Rule
          title="Auto-authorize seedbox IP"
          checked={value.seedbox_ip}
          onChange={(next) => set("seedbox_ip", next)}
          description="Requires a dedicated API session with dynamic seedbox permission. Updates when the route changes or authorization is a day old, no more than once per hour."
        >
          <label>
            Seedbox check interval
            <select
              value={Math.max(3600, value.seedbox_interval_seconds)}
              onChange={(event) =>
                set("seedbox_interval_seconds", Number(event.target.value))
              }
            >
              {[
                3600,
                10800,
                21600,
                43200,
                86400,
                ...(![3600, 10800, 21600, 43200, 86400].includes(
                  Math.max(3600, value.seedbox_interval_seconds),
                )
                  ? [value.seedbox_interval_seconds]
                  : []),
              ].map((seconds) => (
                <option key={seconds} value={seconds}>
                  {seconds / 3600} hours
                </option>
              ))}
            </select>
          </label>
        </Rule>
      </div>
      <button type="submit" className="primary">
        Save MAM settings
      </button>
    </details>
  );
}
