-- Classifier v3 separates "how sure is the zone" (confidence) from "is danger explicit"
-- (explicit_danger), which decides between the fixed crisis reply and a gentle check-in.
ALTER TABLE zone_events ADD COLUMN explicit_danger boolean;
