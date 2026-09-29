import { useSearchParams } from "react-router-dom";
import { decodeCorpusUrlSearch } from "./corpus/queryCodec";
import { filterToday, type DateSelection } from "./filterModel";
export function useUrlFilters(
  keys: readonly string[],
  defaults: Record<string, string> = {},
) {
  const [search, setSearch] = useSearchParams();
  const values = Object.fromEntries(
    keys.map((key) => [key, search.get(key) ?? defaults[key] ?? ""]),
  );
  function patch(patch: Record<string, string>, replace = false) {
    setSearch(
      (old) => {
        const next = new URLSearchParams(old);
        for (const [key, value] of Object.entries(patch)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        return next;
      },
      { replace },
    );
  }
  function reset() {
    setSearch((old) => {
      const next = new URLSearchParams(old);
      for (const key of [...keys, "range", "from", "to"]) next.delete(key);
      return next;
    });
  }
  const dates = decodeCorpusUrlSearch(search.toString(), filterToday());
  function setDates(next: DateSelection) {
    patch({
      range: next.range,
      from: next.range === "custom" ? next.from : "",
      to: next.range === "custom" ? next.to : "",
    });
  }
  return { values, patch, reset, dates, setDates, search };
}
