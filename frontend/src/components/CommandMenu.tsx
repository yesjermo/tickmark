import { useEffect } from "react";
import { Command } from "cmdk";
import { ArrowRight, Bot, CircleDot, FlaskConical, LineChart, Plug, RefreshCw, TestTube2 } from "lucide-react";

export interface PaletteAction { id: string; group: string; label: string; hint?: string; keywords?: string; icon?: "go" | "symbol" | "ws-backtest" | "ws-test" | "ws-trading" | "plug" | "bot" | "refresh"; run: () => void }

const ICONS = { go: ArrowRight, symbol: LineChart, "ws-backtest": FlaskConical, "ws-test": TestTube2, "ws-trading": CircleDot, plug: Plug, bot: Bot, refresh: RefreshCw };

export function CommandMenu({ open, setOpen, actions }: { open: boolean; setOpen: (o: boolean) => void; actions: PaletteAction[] }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); setOpen(!open); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open, setOpen]);
  const groups = [...new Set(actions.map((a) => a.group))];
  return (
    <Command.Dialog open={open} onOpenChange={setOpen} label="Command palette" className="cmdk" overlayClassName="cmdk-overlay">
      <Command.Input placeholder="Jump to a symbol, tab or action…" />
      <Command.List>
        <Command.Empty>No match. Try a ticker, “journal”, “crypto” or “connect”.</Command.Empty>
        {groups.map((g) => (
          <Command.Group key={g} heading={g}>
            {actions.filter((a) => a.group === g).map((a) => {
              const Icon = ICONS[a.icon ?? "go"];
              return (
                <Command.Item key={a.id} value={`${a.label} ${a.keywords ?? ""}`} onSelect={() => { setOpen(false); a.run(); }}>
                  <Icon size={15} aria-hidden /> <span>{a.label}</span>{a.hint && <span className="cmdk-hint">{a.hint}</span>}
                </Command.Item>
              );
            })}
          </Command.Group>
        ))}
      </Command.List>
    </Command.Dialog>
  );
}
