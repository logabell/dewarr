import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import BookDialog from "./BookDialog";
import { Notice } from "../components";
import { api, result } from "../api/client";

export default function DeleteConfiguration({
  name,
  description,
  onDelete,
  onDeleted,
  disabled = false,
  label = "Delete",
}: {
  name: string;
  description: string;
  onDelete: () => Promise<unknown>;
  onDeleted?: () => void;
  disabled?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const cache = useQueryClient();
  const remove = useMutation({
    mutationFn: onDelete,
    onSuccess: async () => {
      setOpen(false);
      onDeleted?.();
      await cache.invalidateQueries();
    },
  });
  return (
    <>
      <button
        type="button"
        disabled={disabled || remove.isPending}
        aria-label={`Delete ${name}`}
        onClick={() => {
          remove.reset();
          setOpen(true);
        }}
      >
        <Trash2 size={15} aria-hidden="true" /> {label}
      </button>
      {open && (
        <BookDialog
          title={`Delete ${name}?`}
          close={() => {
            if (!remove.isPending) setOpen(false);
          }}
        >
          <p>{description}</p>
          <Notice error={remove.error} />
          <div className="button-row">
            <button
              type="button"
              disabled={remove.isPending}
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="danger"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              {remove.isPending ? "Deleting…" : "Delete"}
            </button>
          </div>
        </BookDialog>
      )}
    </>
  );
}

export function DeleteSourceConnection({
  source,
  name,
  generation,
  disabled,
}: {
  source: "mam" | "prowlarr" | "audiobookbay" | "slskd";
  name: string;
  generation: number;
  disabled?: boolean;
}) {
  return (
    <DeleteConfiguration
      name={name}
      disabled={disabled}
      description={
        source === "slskd"
          ? "Remove the Soulseek search and download connection and its saved credentials. Existing files and download history are kept."
          : "Remove this source connection and its saved credentials. You can connect it again later."
      }
      onDelete={async () =>
        result(
          await api.DELETE("/api/sources/{source}/connection", {
            params: {
              path: { source },
              query: { expected_generation: generation },
            },
          }),
        )
      }
    />
  );
}
