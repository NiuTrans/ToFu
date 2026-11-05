---
name: online-spreadsheets
description: Read and edit cloud spreadsheets and smart tables in 腾讯文档 Tencent Docs and 金山文档 Kingsoft Docs (WPS 云文档) through their MCP connectors. Use when the user mentions these products or pastes a docs.qq.com or kdocs.cn link, including to explain how to connect them.
---

# Online spreadsheets: Tencent Docs and Kingsoft Docs

Both products are reached only through their MCP connectors, under the user's
own account: `mcp__tencent-docs__*` and `mcp__kdocs__*`. `fetch_url` of their
document links returns a login page, and sheets render cells on a canvas that
browser page reads cannot extract.

## 1. Find the connector, or explain how to connect it

- On a large tool catalog, call `search_tools` with namespace `tencent-docs`
  or `kdocs`, plus a short query such as `sheet range`.
- If the result carries `capability_availability`, the connector is not
  usable here. Relay its steps to the user: the token page link, then
  Settings → MCP (when the UI-jump directory is present, link it as
  `[打开 MCP 设置](setting:mcp)`). Stop there. Do not retry the search or
  fetch the document until the user says it is connected.
- Kingsoft accepts personal WPS accounts only; enterprise 365.kdocs.cn
  documents need WPS 365 instead.

## 2. Locate the file

- Kingsoft tools take a pasted link directly: pass it as `url` (alternatives:
  `link_id` or `file_id`, exactly one of the three).
- Otherwise resolve the file with the connector's search/list tools. When
  several files match, ask the user rather than guessing.
- Tofu shows Kingsoft's dotted tool names with `_`:
  `sheet.get_range_data` is `mcp__kdocs__sheet_get_range_data`.

## 3. Read before you write

- Grid spreadsheet (表格): list the worksheets first (Kingsoft
  `sheet.get_sheets_info`), then read the header row and only the range the
  task needs. Kingsoft `sheet.get_range_data` takes a 0-based, inclusive
  `{rowFrom, rowTo, colFrom, colTo}` object (no A1 strings), at most 5,000
  rows and 50,000 cells per call; `sheet.get_typed_value` also returns cell
  types. To read a whole document, `read_file` returns Markdown in one call
  (continue with its `task_id` when it says so).
- Smart table (智能表格 / 多维表): read the schema first (Kingsoft
  `dbsheet.get_schema`) for sheet ids, field names and field types; then page
  through records (`dbsheet.list_records`, following `offset`).
- Tencent Docs: discover its tools with `search_tools` and follow each tool's
  description; do not assume Kingsoft's names. Every call counts against the
  account's daily quota (2,000 a day on a free account per the Tencent Docs
  MCP overview, 2026-11), so prefer one larger read over many small ones.

## 4. Write safely

1. Change the smallest range or record set that satisfies the request. Keep
   formulas, formats and unrelated cells; never overwrite a formula cell with
   a value unless asked.
2. Smart tables: values must match the field type from the schema; select
   options must already exist unless the user asked to add them. Updates need
   the record id (Kingsoft `dbsheet.update_records`).
3. Read the written range or records back and report exactly what changed.
4. Get explicit confirmation before deleting rows, records, sheets or files,
   clearing or overwriting non-empty cells, or changing sharing and
   permissions.
5. If a write times out or the connection drops, do not repeat it blindly:
   read back first, because it may already have applied.

## 5. Errors

- Token rejected: Kingsoft answers `{"code":400006,...}` inside the result;
  Tencent fails with HTTP 401 / `invalid or expired token`. Tell the user to
  get a new token (Kingsoft: https://www.kdocs.cn/latest → avatar menu →
  「金山文档Skill」; Tencent: https://docs.qq.com/open/auth/mcp.html) and
  reinstall the connector in Settings → MCP with it.
- Permission errors mean the account cannot access that document; ask the
  owner to share it rather than retrying.
