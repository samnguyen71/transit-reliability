# cols = [c.strip() for c in next(csv.reader([header_line.decode("utf-8-sig")]))]
# cur.execute(sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
#     stg, sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in cols)))
# with cur.copy(sql.SQL("COPY {} FROM STDIN (FORMAT csv, HEADER true)").format(stg)) as cp, \
#      zf.open(name) as f:
#     while chunk := f.read(1 << 20):
#         cp.write(chunk)
# for c in EXPECTED[name]:
#     cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} text")
#                 .format(stg, sql.Identifier(c)))
