import { filterToday } from "./filterModel";
import type { Filters } from "./api";
import {
  buildCorpusApiParams,
  decodeCorpusUrlSearch,
} from "./corpus/queryCodec";

export function dashboardDates(search: URLSearchParams): Filters {
  const filters = decodeCorpusUrlSearch(search.toString(), filterToday());
  const values = buildCorpusApiParams(filters);
  return {
    from: values.from ?? "",
    to: values.to ?? "",
    timezone: values.timezone ?? "Asia/Shanghai",
    bucket: filters.range === "90d" ? "week" : "day",
  };
}
