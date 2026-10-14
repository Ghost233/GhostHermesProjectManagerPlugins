// Native profile preparation, using the original public profile initializer.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const [sdk, home, uiEndpoint, service] = process.argv.slice(2);
fs.mkdirSync(home, {mode: 0o700, recursive: true});
const {initializeProfileFromDefault} = await import(pathToFileURL(path.join(sdk, '@deepseek-ai/dsh/lib/profile-boot.js')));
initializeProfileFromDefault('hermes-owned', 'web', home);
const patch = path.join(home, 'profiles/hermes-owned/cordis.patch.yml');
fs.writeFileSync(patch, JSON.stringify([{insert: [{id: 'fixture-native-approval', name: pathToFileURL(service).href,
  config: {uiEndpoint}}]}]), {mode: 0o600});
