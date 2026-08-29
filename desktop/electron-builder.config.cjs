"use strict";

const packageJson = require("./package.json");
const { withSelectedRuntime } = require("./runtime-selection");

const configured = packageJson.build;

module.exports = withSelectedRuntime(configured, __dirname);
