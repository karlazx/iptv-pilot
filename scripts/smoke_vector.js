/* global db, sleep */
const databaseName = process.env.MONGODB_DATABASE || "iptv_manager";
const appDb = db.getSiblingDB(databaseName);
const collectionName = "phase0_vector_smoke";
const indexName = "phase0_vector_index";
const collection = appDb.getCollection(collectionName);

collection.drop();

function vector(x, y) {
  const values = Array(1024).fill(0);
  values[0] = x;
  values[1] = y;
  return values;
}

collection.insertMany([
  {label: "expected-nearest", embedding: vector(1, 0)},
  {label: "orthogonal", embedding: vector(0, 1)}
]);

collection.createSearchIndex(indexName, "vectorSearch", {
  fields: [{type: "vector", path: "embedding", numDimensions: 1024, similarity: "cosine"}]
});

let ready = false;
for (let attempt = 0; attempt < 90; attempt += 1) {
  const indexes = collection.getSearchIndexes(indexName);
  const current = Array.isArray(indexes) ? indexes : indexes.toArray();
  if (current.length && current[0].queryable === true) {
    ready = true;
    break;
  }
  sleep(2000);
}

if (!ready) throw new Error("vector smoke index did not become queryable");

const results = collection.aggregate([
  {
    $vectorSearch: {
      index: indexName,
      path: "embedding",
      queryVector: vector(1, 0),
      numCandidates: 2,
      limit: 1
    }
  },
  {$project: {_id: 0, label: 1, score: {$meta: "vectorSearchScore"}}}
]).toArray();

if (results.length !== 1 || results[0].label !== "expected-nearest") {
  throw new Error(`unexpected vector search result: ${JSON.stringify(results)}`);
}

printjson({ok: 1, result: results[0]});
collection.drop();
