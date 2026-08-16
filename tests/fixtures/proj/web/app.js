const { fmt } = require("./util.js");

function greet(name) {
  return fmt("hi ", name);
}

module.exports = { greet };
