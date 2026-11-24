const { readFileSync } = require('node:fs');
const panel = require('../../site/js/panel-minute.js');
const { hourly, minute, cutoff } = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(panel.rows('crypto', hourly, minute, Date.parse(cutoff))));
