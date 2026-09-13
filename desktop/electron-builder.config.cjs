"use strict";

const packageJson = require("./package.json");
const { withSelectedRuntime } = require("./runtime-selection");
const { withPreservedKestraJava } = require("./kestra-signing.cjs");

const configured = packageJson.build;

module.exports = withPreservedKestraJava(withSelectedRuntime(configured, __dirname), __dirname);
