---
name: JAV Pilot
description: 以现代产品 UX 为基础，参考 peach 的材质表现，优先内容层级与操作效率。
colors:
  primary: '#0866df'
  background: '#edf1f8'
  surface: '#ffffff'
  ink: '#171717'
  muted: '#666666'
  border: '#e5e5e5'
  dark-background: '#0d1119'
  dark-surface: '#181818'
  dark-ink: '#ededed'
  dark-primary: '#8dbaff'
rounded:
  control: '8px'
  surface: '16px'
  navigation: '20px'
spacing:
  small: '8px'
  medium: '16px'
  large: '32px'
typography:
  body:
    fontFamily: 'Segoe UI, Noto Sans SC, Microsoft YaHei, sans-serif'
    fontSize: '14px'
    fontWeight: 400
    lineHeight: 1.5
  heading:
    fontFamily: 'Segoe UI, Noto Sans SC, Microsoft YaHei, sans-serif'
    fontSize: '28px'
    fontWeight: 600
    lineHeight: 1.25
---

## Overview

面向高频检索、下载与媒体管理的操作界面。参考 https://github.com/peach-mitao/peach 的毛玻璃材质、紧凑导航与内容优先布局；保留 JAV Pilot 原有工作流和语义状态。样式源为 `frontend/src/styles/tokens.css`，组件与页面继续使用现有 CSS 模块文件。

## Colors

亮色采用半透明白色内容面与冷灰背景；暗色采用半透明蓝灰内容面与近黑背景。背景以低饱和冷色和暖色映光建立空间。导航和搜索类型选中态使用中性灰，蓝色用于主操作、焦点、链接及开启状态。危险、警告、成功保持各自的语义色，不借用导航选中色。

## Typography

沿用系统无衬线字体和中文回退。桌面页面标题 28px/600，正文 14px，辅助说明 12px。移动页面继续使用各页面既有的紧凑字号。数据和表单标签优先清晰，不添加装饰性标题。

## Layout

桌面导航与窗口边缘相距 12px，常规占宽 232px，收起时占宽 80px。收起后的图标链接保留辅助技术名称与悬停标题；偏好保存在本地，存储不可用时仍可切换。桌面内容上方为路径与导航切换条。900px 及以下使用原有顶部工具栏与底部分组导航。

搜索使用单列工作区：上方查询与可展开筛选，下方结果。筛选默认收起，保留已选参数；无效字段会展开所在筛选区。桌面三个基础参数并列，移动端沿用紧凑排布。

## Elevation & Depth

材质由 `styles/materials.css` 统一定义，参数在 `styles/tokens.css`。导航与浮层使用 20–28px 背景模糊、半透明底色、边缘高光及柔和投影。内容面板采用稳定、轻微透明的填充，不叠加模糊或镜面映光。正文、输入框和海报保持清晰。支持降低透明度偏好；无模糊支持时导航退回实色。

## Shapes

控件圆角 8px，内容表面 16px，浮置桌面导航 20px。图标沿用 Lucide，保持既有线条风格。

## Components

导航选中态由玻璃高光、边框和文字共同表达。搜索类型使用分段式选中面。主按钮继续使用蓝色，次级按钮使用中性表面。键盘焦点、文本选择、输入光标与滚动条均使用主题变量。明暗主题的初始 HTML 和 React 元信息保持一致，避免首帧闪烁。

## Do's and Don'ts

- 新页面复用现有 token、表单及状态组件。
- 保持业务内容、来源信息与操作反馈，不为视觉效果伪造数据。
- 优先让当前任务和结果占据空间，次级参数渐进展开。
- 不引入新的 UI 框架、历史兼容层或重复的主题实现。

目录封面的九宫格只展示海报。番号、作品数量和编辑按钮位于图片下方的独立布局区域，数量占整行；长番号允许换行。44px 编辑按钮保留完整触控面积，不向封面延伸热区。

媒体库画廊直接呈现在页面底上，筛选栏独立成面，避免大容器套住所有海报。手机目录采用两列保证九宫格预览的可读尺寸；查询与提交按钮同排。设置、搜索类型与分段控件共享选中面语言。玻璃只服务导航和浮层的空间层级，不作为所有组件的统一装饰。
