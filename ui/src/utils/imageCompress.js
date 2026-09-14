// 图片上传前压缩（浏览器原生 Canvas，无第三方依赖）
//
// 策略：
// - 最长边超过 MAX_DIMENSION 时等比缩小（DeepSeek 视觉对 2048px 已足够清晰）
// - JPEG/WebP 体积超过 REENCODE_BYTES 时即使尺寸不超也重编码降质量
// - PNG 只缩放不换格式（截图/图表含文字与透明通道，换 JPEG 会糊且丢透明）
// - GIF 完全不处理（Canvas 只取首帧会丢失动画）
// - 任何解码/编码异常都回退原文件，压缩绝不能阻断上传链路

export const MAX_DIMENSION = 2048
export const REENCODE_BYTES = 2 * 1024 * 1024 // 超过 2MB 的 JPEG/WebP 重编码
const JPEG_QUALITY = 0.85
const WEBP_QUALITY = 0.82

function replaceExt(name, ext) {
  const dot = name.lastIndexOf('.')
  const base = dot > 0 ? name.slice(0, dot) : name
  return `${base}.${ext}`
}

function canvasToBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('canvas.toBlob 返回空'))),
      type,
      quality
    )
  })
}

/**
 * 压缩单张图片。
 * @param {File} file 原始图片文件
 * @returns {Promise<File>} 压缩后的 File；无需处理或处理失败时返回原 File
 */
export async function compressImage(file) {
  // GIF 动图：跳过
  if (file.type === 'image/gif') return file

  const needScaleBySize = file.size > REENCODE_BYTES
  let bitmap = null
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    // 浏览器无法解码（如极个别 WebP），不做处理
    return file
  }

  try {
    const longest = Math.max(bitmap.width, bitmap.height)
    const scale = Math.min(1, MAX_DIMENSION / longest)
    const needResize = scale < 1

    // 尺寸不超限：仅 JPEG/WebP 大文件重编码，PNG 与小图原样返回（避免重复压缩降质）
    if (!needResize) {
      if (!needScaleBySize || file.type === 'image/png') return file
    }

    const targetW = Math.max(1, Math.round(bitmap.width * scale))
    const targetH = Math.max(1, Math.round(bitmap.height * scale))

    // 输出类型：PNG 保持 PNG；其余统一 JPEG（兼容性最好，且 DeepSeek 支持）
    let outType = 'image/jpeg'
    let outExt = 'jpg'
    let quality = JPEG_QUALITY
    if (file.type === 'image/png') {
      outType = 'image/png'
      outExt = 'png'
      quality = undefined
    } else if (file.type === 'image/webp') {
      outType = 'image/webp'
      outExt = 'webp'
      quality = WEBP_QUALITY
    }

    const canvas = document.createElement('canvas')
    canvas.width = targetW
    canvas.height = targetH
    const ctx = canvas.getContext('2d')
    if (file.type === 'image/jpeg' || outType === 'image/jpeg') {
      // JPEG 无透明通道，先铺白底，避免透明区域被编码成黑色
      ctx.fillStyle = '#fff'
      ctx.fillRect(0, 0, targetW, targetH)
    }
    ctx.drawImage(bitmap, 0, 0, targetW, targetH)

    const blob = await canvasToBlob(canvas, outType, quality)

    // 极端情况下压缩后反而更大（PNG 简单图等），保留原文件
    if (blob.size >= file.size && !needResize) return file

    return new File([blob], replaceExt(file.name, outExt), {
      type: outType,
      lastModified: Date.now()
    })
  } catch {
    return file
  } finally {
    bitmap.close()
  }
}
