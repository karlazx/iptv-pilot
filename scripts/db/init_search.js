/* global db, sleep */
const uri = process.env.MONGODB_URI;
const databaseName = process.env.MONGODB_DATABASE || "iptv_pilot";
if (!uri) throw new Error("MONGODB_URI is required");

const connection = new Mongo(uri);
const appDb = connection.getDB(databaseName);

function ensureVectorIndex(collectionName, indexName) {
  const collection = appDb.getCollection(collectionName);
  const searchIndexes = collection.getSearchIndexes(indexName);
  const existing = Array.isArray(searchIndexes) ? searchIndexes : searchIndexes.toArray();
  if (existing.length) return;
  collection.createSearchIndex(
    indexName,
    "vectorSearch",
    {fields: [{type: "vector", path: "embedding", numDimensions: 1024, similarity: "cosine"}]}
  );
}

let lastError;
for (let attempt = 0; attempt < 60; attempt += 1) {
  try {
    ensureVectorIndex("transcripts", "transcript_vector");
    ensureVectorIndex("summaries", "summary_vector");
    print("MongoDB Search indexes initialized");
    quit(0);
  } catch (error) {
    lastError = error;
    print(`mongot not ready (${attempt + 1}/60): ${error}`);
    sleep(5000);
  }
}
throw lastError;
