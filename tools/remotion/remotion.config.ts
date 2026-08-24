import {Config} from '@remotion/cli/config';

/**
 * Render defaults for this tool. The CLI reads this file automatically, so
 * `npx remotion render PedazoDeManco out/pedazo-de-manco.mp4` is enough - the
 * entry point and codec do not have to be repeated on the command line.
 *
 * Deliberately NOT set here: concurrency. This machine is shared with other
 * desks, so the caller passes `--concurrency N` explicitly and says what they
 * used. See README.md.
 */
Config.setEntryPoint('./src/index.ts');
Config.setVideoImageFormat('jpeg');
Config.setCodec('h264');
Config.setCrf(16);
Config.setOverwriteOutput(true);
