import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { UploadSelect, type UploadAmount } from "./MamAutomation";
import { randomUUID } from "../randomUUID";
import "./mam-control-center.css";

type Connection = components["schemas"]["MAMConnectionView"];
type Purchase = components["schemas"]["MAMPurchase"];
const fmt = (value: number) =>
  value.toLocaleString(undefined, { maximumFractionDigits: 2 });

export default function MamControlCenter({
  connection,
  blocked,
  onBusy,
}: {
  connection: Connection;
  blocked: boolean;
  onBusy: (busy: boolean) => void;
}) {
  const [amount, setAmount] = useState<UploadAmount>(50);
  const [selected, setSelected] = useState<Purchase["kind"]>("upload");
  const [review, setReview] = useState<Purchase | null>(null);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const submitting = useRef(false);
  const ready = connection.enabled && connection.has_session && !blocked;
  const account = useQuery({
    queryKey: ["mam-account", connection.generation],
    queryFn: async () =>
      result(
        await api.GET("/api/sources/mam/account", {
          params: { query: { expected_generation: connection.generation } },
        }),
      ),
    enabled: ready,
    staleTime: 60000,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const purchase = useMutation({
    mutationFn: async (body: Purchase) =>
      result(await api.POST("/api/sources/mam/purchases", { body })),
    retry: false,
    onSettled: () => {
      setReview(null);
      setRefreshRequired(true);
      void account.refetch();
      submitting.current = false;
    },
  });
  const busy = account.isFetching || purchase.isPending;
  useEffect(() => {
    onBusy(busy);
    return () => onBusy(false);
  }, [busy, onBusy]);
  useEffect(() => {
    if (blocked) setReview(null);
  }, [blocked]);
  const data = ready ? account.data : undefined;
  const points = data?.seedbonus;
  const validAmount =
    amount === "max" ||
    (Number.isInteger(amount) && amount >= 50 && amount <= 100000);
  const cost =
    selected === "upload"
      ? amount === "max"
        ? null
        : amount * 500
      : selected === "wedges"
        ? 50000
        : null;
  const minimum =
    selected === "VIP"
      ? 1250
      : selected === "wedges"
        ? 50000
        : amount === "max"
          ? 25000
          : cost!;
  const affordable = points != null && points >= minimum;
  const vipRoom =
    !data?.vip_until ||
    new Date(data.vip_until).getTime() - Date.now() <= 83 * 86400000;
  const canBuy =
    ready &&
    !!data &&
    !account.isError &&
    !busy &&
    affordable &&
    (selected !== "upload" || validAmount) &&
    (selected !== "VIP" || vipRoom) &&
    !refreshRequired;
  const title =
    selected === "upload"
      ? "Buy upload credit"
      : selected === "VIP"
        ? "Max out VIP"
        : "Buy a Freeleech wedge";

  return (
    <section className="panel mam-control" aria-label="MAM control center">
      <header className="mam-control-heading">
        <div>
          <p className="eyebrow">MAM ACCOUNT</p>
          <h3>{data?.username || "Your MAM account"}</h3>
          <p className="muted">
            {data
              ? [
                  data.classname || "Class unavailable",
                  `User ${data.uid}`,
                ].join(" · ")
              : "Account balances and point purchases"}
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={!ready || busy}
          onClick={async () => {
            const refreshed = await account.refetch();
            if (refreshed.isSuccess) setRefreshRequired(false);
          }}
        >
          {account.isFetching ? "Refreshing…" : "Refresh account"}
        </button>
      </header>
      {!ready && (
        <p className="mam-control-note">
          {blocked
            ? "Save connection changes to load the matching MAM account."
            : "Enable MAM and save a session cookie to see your account."}
        </p>
      )}
      {ready && account.isError && (
        <p role="alert" className="mam-control-note">
          {account.error.message}{" "}
          {data
            ? "The values below are from the last successful refresh."
            : "Use Refresh account to try again."}
        </p>
      )}
      {ready && account.isPending && (
        <p role="status">Loading your MAM account…</p>
      )}
      <dl className="mam-balances">
        {[
          ["Ratio", data?.ratio],
          ["Bonus points", points == null ? null : fmt(points)],
          ["Uploaded", data?.uploaded],
          ["Downloaded", data?.downloaded],
        ].map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value ?? "Unavailable"}</dd>
          </div>
        ))}
      </dl>
      {data && (
        <p className="mam-account-timestamp">
          Last refreshed{" "}
          {data.checked_at
            ? new Date(data.checked_at).toLocaleString()
            : "Unknown"}
          {data.vip_until
            ? ` · VIP expires ${new Date(data.vip_until).toLocaleDateString()}`
            : " · VIP expiry unavailable"}
        </p>
      )}
      <div className="mam-store-heading">
        <h4>One-time purchase</h4>
        <span>Uses bonus points</span>
      </div>
      <fieldset className="mam-store" disabled={!ready || busy || !!review}>
        <label>
          Purchase
          <select
            value={selected}
            onChange={(event) => {
              setSelected(event.target.value as Purchase["kind"]);
              purchase.reset();
            }}
          >
            <option value="upload">Upload credit</option>
            <option value="VIP">VIP status · max top-up</option>
            <option value="wedges">Freeleech wedge · 50,000 points</option>
          </select>
        </label>
        {selected === "upload" ? (
          <UploadSelect
            label="Upload credit amount"
            value={amount}
            onChange={(next) => {
              setAmount(next);
              purchase.reset();
            }}
          />
        ) : (
          <p className="mam-purchase-description">
            {selected === "VIP"
              ? "Extends VIP toward the 90-day limit, subject to available points and MAM’s rank requirements. Minimum purchase: seven days (1,250 points)."
              : "Buys a wedge for later use. This does not apply it to a torrent. Cheese payment is not documented by the API."}
          </p>
        )}
      </fieldset>
      <div className="mam-exchange" aria-live="polite">
        <span>
          {selected === "upload" && !validAmount
            ? "Enter a valid upload amount"
            : cost != null && Number.isFinite(cost)
              ? `${fmt(cost)} points`
              : selected === "upload"
                ? "All affordable upload credit"
                : "Maximum eligible VIP top-up"}
        </span>
        <span>
          {points == null
            ? "Refresh to check your balance"
            : cost != null && Number.isFinite(cost)
              ? `${fmt(Math.max(0, points - cost))} points left${points < cost ? " · insufficient balance" : ""}`
              : "Variable cost · may use all available points"}
        </span>
      </div>
      {selected === "upload" && (
        <p className="muted">
          500 points per GiB. MAM’s API requires whole amounts of at least 50
          GiB; the smaller website options are unavailable here.
        </p>
      )}
      {selected === "VIP" && !vipRoom && (
        <p role="status">
          VIP is too close to the 90-day limit for a seven-day top-up.
        </p>
      )}
      {selected === "upload" && !validAmount && (
        <p role="status">Enter a whole number from 50 to 100,000 GiB.</p>
      )}
      {!affordable && data && (selected !== "upload" || validAmount) && (
        <p role="status">
          {points == null
            ? "MAM did not report a points balance."
            : `Requires at least ${fmt(minimum)} points.`}
        </p>
      )}
      {review ? (
        <div
          className="mam-purchase-review"
          role="group"
          aria-label="Review MAM purchase"
        >
          <strong>{title}?</strong>
          <p>
            {selected === "upload" && amount !== "max"
              ? `${amount} GiB for ${fmt(amount * 500)} points.`
              : selected === "wedges"
                ? "One wedge for 50,000 points."
                : "MAM determines the maximum purchase from your balance and account limits."}{" "}
            This is a one-time action.
          </p>
          <div className="actions">
            <button
              type="button"
              disabled={!canBuy}
              onClick={() => {
                if (!submitting.current && canBuy) {
                  submitting.current = true;
                  purchase.mutate(review);
                }
              }}
            >
              {purchase.isPending ? "Purchasing…" : "Confirm purchase"}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={purchase.isPending}
              onClick={() => setReview(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          disabled={!canBuy}
          onClick={() =>
            setReview({
              request_id: randomUUID(),
              expected_generation: connection.generation,
              kind: selected,
              ...(selected === "upload" ? { amount } : {}),
            })
          }
        >
          Review purchase
        </button>
      )}
      {purchase.data && (
        <p
          role="status"
          className="mam-control-note"
          data-outcome={purchase.data.status}
        >
          {purchase.data.message}
        </p>
      )}
      {purchase.error && (
        <p role="alert" className="mam-control-note">
          {purchase.error.message} If the connection was interrupted, check MAM
          before trying again.
        </p>
      )}
      {refreshRequired && (
        <p className="muted">
          Review the result, then refresh your account before starting another
          purchase.
        </p>
      )}
      <p className="mam-control-footnote">
        Seedtime fixes, cheese payments, and fixed VIP durations are available
        on the MAM website; they are not documented purchase options in this
        API.
      </p>
    </section>
  );
}
