import { createDecipheriv, createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { spawnSync } from 'node:child_process';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
if (args.length !== 4 || args[0] !== '--key-file' || args[2] !== '--output') {
  console.error('Usage: node restore.mjs --key-file /path/to/key --output /new/project');
  process.exit(1);
}
const meta = JSON.parse(await readFile(join(here, 'snapshot.json'), 'utf8'));
const key = await readFile(resolve(args[1]));
if (key.length !== 32) throw new Error('Expected a 32-byte binary key file');
const scratch = await mkdtemp(join(tmpdir(), 'jobfinder-restore-'));
try {
  async function* chunks() {
    for (const part of meta.parts) {
      if (!/^archive\.part\d{3}$/.test(part.name)) throw new Error('Invalid part name');
      const hash = createHash('sha256');
      let size = 0;
      for await (const bytes of createReadStream(join(here, part.name))) {
        hash.update(bytes); size += bytes.length; yield bytes;
      }
      if (size !== part.bytes || hash.digest('hex') !== part.sha256) {
        throw new Error(`Corrupt part: ${part.name}`);
      }
    }
  }
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(meta.nonce, 'hex'));
  decipher.setAuthTag(Buffer.from(meta.tag, 'hex'));
  const archive = join(scratch, 'project.tar.gz');
  // Extraction starts only after successful authentication of the entire archive.
  await pipeline(Readable.from(chunks()), decipher, createWriteStream(archive, { mode: 0o600 }));
  const result = spawnSync('python3', [join(here, 'verify-extract.py'), archive, resolve(args[3])], { stdio: 'inherit' });
  if (result.status !== 0) throw new Error('Restore or verification failed');
} finally {
  key.fill(0);
  await rm(scratch, { recursive: true, force: true });
}
