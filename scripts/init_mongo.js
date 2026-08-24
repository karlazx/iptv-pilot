/* global db, rs, sleep */
const databaseName = process.env.MONGODB_DATABASE || "iptv_pilot";
const appUser = process.env.IPTV_DB_USER || "iptv_user";
const appPassword = process.env.IPTV_DB_PASSWORD || "change-me-iptv";
const mongotPassword = process.env.MONGOT_PASSWORD || "change-me-mongot";

try {
  rs.status();
} catch (error) {
  print("initializing single-node replica set rs0");
  rs.initiate({_id: "rs0", members: [{_id: 0, host: "mongodb:27017"}]});
}

for (let attempt = 0; attempt < 60; attempt += 1) {
  if (db.hello().isWritablePrimary) break;
  sleep(1000);
}
if (!db.hello().isWritablePrimary) throw new Error("replica set did not elect a primary");

const adminDb = db.getSiblingDB("admin");
if (!adminDb.getUser("mongotUser")) {
  adminDb.createUser({user: "mongotUser", pwd: mongotPassword, roles: ["searchCoordinator"]});
}

const appDb = db.getSiblingDB(databaseName);
if (!appDb.getUser(appUser)) {
  appDb.createUser({user: appUser, pwd: appPassword, roles: [{role: "readWrite", db: databaseName}]});
}

const collections = [
  "channels", "epg_programs", "subscriptions", "recordings", "transcripts", "summaries", "bookmarks", "push_events"
];
for (const name of collections) {
  if (!appDb.getCollectionNames().includes(name)) appDb.createCollection(name);
}

appDb.channels.createIndex({channel_id: 1}, {unique: true, name: "channel_id_unique"});
appDb.channels.createIndex({category: 1}, {name: "category"});
appDb.epg_programs.createIndex({channel_id: 1, start_time: 1}, {unique: true, name: "channel_start_unique"});
appDb.epg_programs.createIndex({start_time: 1}, {expireAfterSeconds: 7776000, name: "epg_90d_ttl"});
appDb.epg_programs.createIndex({title: "text", description: "text"}, {name: "epg_text", default_language: "none"});
appDb.subscriptions.createIndex({match_type: 1, enabled: 1}, {name: "subscription_match"});
appDb.recordings.createIndex({status: 1, scheduled_start: 1}, {name: "recording_queue"});
appDb.recordings.createIndex({subscription_id: 1, start_time: -1}, {name: "subscription_history"});
appDb.recordings.createIndex({subscription_id: 1, start_time: 1}, {unique: true, name: "recording_dedupe"});
appDb.transcripts.createIndex({recording_id: 1}, {unique: true, name: "transcript_recording_unique"});
appDb.summaries.createIndex({recording_id: 1}, {unique: true, name: "summary_recording_unique"});
appDb.summaries.createIndex({pushed: 1, created_at: -1}, {name: "summary_push_queue"});
appDb.summaries.createIndex(
  {summary_text: "text", key_points: "text", entities: "text"},
  {name: "summary_text", default_language: "none"}
);
appDb.bookmarks.createIndex({user: 1, item_type: 1, item_id: 1}, {unique: true, name: "bookmark_unique"});
appDb.push_events.createIndex({summary_id: 1, provider: 1}, {unique: true, name: "push_event_unique"});
appDb.push_events.createIndex({status: 1, updated_at: -1}, {name: "push_event_status"});

print(`MongoDB initialization complete: ${databaseName}`);
