// Executes a pipeline with the JS "mingo" engine. Used only by tests/check_pipelines.py, which has no
// MongoDB server to run against. usage: node mingo_runner.js job.json  (needs `npm i mingo@6`)
const fs = require("fs");
const { Aggregator } = require("mingo/aggregator");
require("mingo/init/system"); // registers every operator

const revive = (v) => {
  if (Array.isArray(v)) return v.map(revive);
  if (v && typeof v === "object") {
    if (Object.keys(v).length === 1 && "__date__" in v) return new Date(v.__date__);
    const o = {};
    for (const k of Object.keys(v)) o[k] = revive(v[k]);
    return o;
  }
  return v;
};
const job = revive(JSON.parse(fs.readFileSync(process.argv[2], "utf8")));

// mingo 6 runs a $lookup sub-pipeline over the WHOLE foreign collection when localField/foreignField and
// `pipeline` are combined (MongoDB joins first, then runs the pipeline on the matching documents).
// Rewrite that form into the equivalent `let` + $expr/$eq form, which mingo executes correctly.
const rewrite = (pipeline) =>
  pipeline.map((st) => {
    const lk = st.$lookup;
    if (lk && lk.localField && lk.foreignField && lk.pipeline) {
      return {
        $lookup: {
          from: lk.from,
          let: { __local: "$" + lk.localField },
          pipeline: [{ $match: { $expr: { $eq: ["$" + lk.foreignField, "$$__local"] } } }, ...lk.pipeline],
          as: lk.as,
        },
      };
    }
    return st;
  });
job.pipeline = rewrite(job.pipeline);
const out = new Aggregator(job.pipeline, {
  collectionResolver: (name) => job.collections[name] || [],
}).run(job.collections[job.collection]);
console.log(JSON.stringify(out, (k, v) => (v instanceof Date ? { __date__: v.toISOString() } : v)));
