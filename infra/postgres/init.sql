-- Runs once when the local Postgres volume is first created.
-- Separate database for the automated test-suite so tests never touch dev data.
CREATE DATABASE family_budget_test;
