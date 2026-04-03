#!/usr/bin/env python3
"""
Browser Advanced Tools Demo

演示如何使用新增的浏览器高级操作功能。
运行此脚本前，确保：
1. 浏览器扩展已连接到服务器
2. 有一个打开的标签页用于测试
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.browser import send_browser_command, is_extension_connected


def demo_get_elements(tab_id):
    """演示：获取页面交互元素"""
    print("\n" + "="*60)
    print("📋 演示 1: 获取页面交互元素")
    print("="*60)
    
    result, error = send_browser_command('get_interactive_elements', {
        'tabId': tab_id,
        'viewport': True,
        'maxElements': 50
    }, timeout=5)
    
    if error:
        print(f"❌ 错误：{error}")
        return None
    
    elements = result.get('elements', [])
    print(f"✅ 找到 {len(elements)} 个交互元素")
    
    # 显示前 10 个
    for i, el in enumerate(elements[:10], 1):
        print(f"  {i}. [{el.get('tag', '?')}] {el.get('text', '')[:50]}")
        print(f"     选择器：{el.get('selector', '?')}")
    
    return elements


def demo_hover(tab_id, selector):
    """演示：悬停元素"""
    print("\n" + "="*60)
    print(f"🖱️  演示 2: 悬停元素 - {selector}")
    print("="*60)
    
    result, error = send_browser_command('hover_element', {
        'tabId': tab_id,
        'selector': selector
    }, timeout=5)
    
    if error or not result.get('hovered'):
        print(f"❌ 悬停失败：{error or result}")
        return False
    
    print(f"✅ 悬停成功：{result}")
    return True


def demo_keyboard(tab_id, keys, selector=None):
    """演示：键盘输入"""
    print("\n" + "="*60)
    print(f"⌨️  演示 3: 键盘输入 - {keys}")
    print("="*60)
    
    params = {'tabId': tab_id, 'keys': keys}
    if selector:
        params['selector'] = selector
    
    result, error = send_browser_command('keyboard_input', params, timeout=5)
    
    if error or not result.get('success'):
        print(f"❌ 键盘输入失败：{error or result}")
        return False
    
    print(f"✅ 键盘输入成功：{result}")
    return True


def demo_wait(tab_id, selector, condition='visible'):
    """演示：等待元素"""
    print("\n" + "="*60)
    print(f"⏳ 演示 4: 等待元素 - {selector} ({condition})")
    print("="*60)
    
    result, error = send_browser_command('wait_for_element', {
        'tabId': tab_id,
        'selector': selector,
        'condition': condition,
        'timeout': 5000
    }, timeout=8)
    
    if error:
        print(f"❌ 等待失败：{error}")
        return False
    
    if result.get('found'):
        print(f"✅ 元素找到！等待了 {result.get('waited_ms', 'N/A')}ms")
        return True
    else:
        print(f"❌ 元素未在超时时间内出现")
        return False


def demo_right_click(tab_id, selector):
    """演示：右键点击"""
    print("\n" + "="*60)
    print(f"🖱️  演示 5: 右键点击 - {selector}")
    print("="*60)
    
    result, error = send_browser_command('click_element', {
        'tabId': tab_id,
        'selector': selector,
        'rightClick': True,
        'scrollTo': True
    }, timeout=5)
    
    if error or not result.get('clicked'):
        print(f"❌ 右键失败：{error or result}")
        return False
    
    print(f"✅ 右键成功：{result}")
    return True


def demo_advanced_right_click_menu(tab_id):
    """演示：高级右键菜单操作"""
    print("\n" + "="*60)
    print("🎯 演示 6: 高级右键菜单操作 (复合功能)")
    print("="*60)
    
    # 导入高级工具
    from lib.browser.advanced import right_click_menu_select
    
    # 这个演示需要实际的菜单元素，这里只展示 API 调用
    print("示例调用:")
    print("""
    from lib.browser.advanced import right_click_menu_select
    
    result = right_click_menu_select(
        tab_id=123,
        target_selector="#target-element",
        menu_item_text="Actions",
        submenu_item_text="Task List",
        menu_wait=0.5,
        timeout=5.0
    )
    
    if result['success']:
        print(f"✅ 完成，耗时 {result['elapsed_ms']}ms")
        print(f"   步骤：{result['steps_completed']}")
    else:
        print(f"❌ 失败：{result['error']}")
        if 'available_items' in result:
            print(f"   可用菜单项：{result['available_items']}")
    """)
    
    print("\n⚠️  此演示需要实际的页面元素，跳过实际执行")
    return True


def demo_advanced_hover_click(tab_id):
    """演示：高级悬停点击"""
    print("\n" + "="*60)
    print("🎯 演示 7: 高级悬停点击 (复合功能)")
    print("="*60)
    
    from lib.browser.advanced import hover_and_click
    
    print("示例调用:")
    print("""
    from lib.browser.advanced import hover_and_click
    
    result = hover_and_click(
        tab_id=123,
        hover_selector="nav .dropdown",
        click_selector="nav .dropdown-menu a",
        hover_wait=0.3
    )
    
    if result['success']:
        print(f"✅ 完成，耗时 {result['elapsed_ms']}ms")
    else:
        print(f"❌ 失败：{result['error']}")
    """)
    
    print("\n⚠️  此演示需要实际的页面元素，跳过实际执行")
    return True


def main():
    print("""
╔═══════════════════════════════════════════════════════════╗
║        Browser Advanced Tools Demo                        ║
║        浏览器高级操作功能演示                              ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    # 检查扩展连接
    if not is_extension_connected():
        print("❌ 浏览器扩展未连接！")
        print("请确保：")
        print("  1. 浏览器扩展已安装")
        print("  2. 扩展已连接到服务器")
        print("  3. 服务器正在运行")
        sys.exit(1)
    
    print("✅ 浏览器扩展已连接")
    
    # 获取标签页列表
    result, error = send_browser_command('list_tabs', {}, timeout=5)
    if error:
        print(f"❌ 获取标签页失败：{error}")
        sys.exit(1)
    
    tabs = result if isinstance(result, list) else []
    if not tabs:
        print("❌ 没有打开的标签页")
        sys.exit(1)
    
    print(f"\n📑 找到 {len(tabs)} 个标签页:")
    for i, tab in enumerate(tabs[:5], 1):
        print(f"  {i}. [{tab['id']}] {tab['title'][:60]}")
        print(f"     URL: {tab['url'][:80]}")
    
    # 选择第一个标签页进行测试
    tab_id = tabs[0]['id']
    print(f"\n👉 使用标签页 {tab_id} 进行演示")
    
    # 运行演示
    elements = demo_get_elements(tab_id)
    
    if elements and len(elements) > 0:
        # 使用第一个元素演示悬停
        first_selector = elements[0].get('selector')
        if first_selector:
            demo_hover(tab_id, first_selector)
            demo_right_click(tab_id, first_selector)
    
    # 演示键盘输入（Escape 关闭可能的菜单）
    demo_keyboard(tab_id, 'Escape')
    
    # 演示等待
    demo_wait(tab_id, 'body')
    
    # 演示高级功能
    demo_advanced_right_click_menu(tab_id)
    demo_advanced_hover_click(tab_id)
    
    print("\n" + "="*60)
    print("✅ 所有演示完成！")
    print("="*60)
    print("\n💡 查看 README_BROWSER_ENHANCEMENTS.md 获取完整使用文档")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⛔ 用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 错误：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
