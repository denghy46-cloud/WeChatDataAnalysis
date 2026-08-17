import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const settingsSource = await readFile(new URL('../components/SettingsDialog.vue', import.meta.url), 'utf8')
const apiSource = await readFile(new URL('../composables/useApi.js', import.meta.url), 'utf8')

test('settings exposes a reversible macOS WeChat update guard', () => {
  assert.match(settingsSource, /阻止微信自动升级/)
  assert.match(settingsSource, /wechatUpdateGuardDescription/)
  assert.match(settingsSource, /toggleWechatUpdateGuard/)
  assert.match(settingsSource, /不修改微信程序和官方签名，可随时恢复/)
})

test('frontend API includes update guard status and toggle endpoints', () => {
  assert.match(apiSource, /\/system\/wechat_update_guard\/status/)
  assert.match(apiSource, /\/system\/wechat_update_guard\/toggle/)
})
