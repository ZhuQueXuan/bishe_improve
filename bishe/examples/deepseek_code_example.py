import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# DeepSeek API 配置
load_dotenv(Path(__file__).resolve().parents[1] / 'knowledge_graph_backend' / '.env')
api_key = os.getenv('DEEPSEEK_API_KEY')
api_base_url = os.getenv('DEEPSEEK_API_BASE_URL', 'https://api.deepseek.com')
if not api_key:
    raise RuntimeError('DEEPSEEK_API_KEY is not configured in the backend .env file')

# 初始化客户端
client = OpenAI(
    api_key=api_key,
    base_url=api_base_url
)

# 示例1: 代码生成
def generate_code():
    """基本的代码生成示例"""
    print("示例1: 代码生成")
    print("-" * 50)
    
    prompt = """
    请编写一个Python函数，实现快速排序算法。
    函数应该接受一个列表作为输入，并返回排序后的列表。
    请包含详细的注释来解释算法的工作原理。
    """
    
    response = client.chat.completions.create(
        model="deepseek-coder",  # 使用DeepSeek的代码模型
        messages=[
            {"role": "system", "content": "你是一个专业的编程助手，擅长编写高质量、可读性强的代码。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,  # 较低的温度，使输出更加确定性
        max_tokens=1000
    )
    
    print(response.choices[0].message.content)
    print("\n")

# 示例2: 代码解释
def explain_code():
    """代码解释示例"""
    print("示例2: 代码解释")
    print("-" * 50)
    
    code_to_explain = """
    def binary_search(arr, target):
        left, right = 0, len(arr) - 1
        while left <= right:
            mid = (left + right) // 2
            if arr[mid] == target:
                return mid
            elif arr[mid] < target:
                left = mid + 1
            else:
                right = mid - 1
        return -1
    """
    
    prompt = f"请解释以下代码的工作原理，包括算法复杂度分析：\n{code_to_explain}"
    
    response = client.chat.completions.create(
        model="deepseek-coder",
        messages=[
            {"role": "system", "content": "你是一个专业的编程教师，擅长解释代码和算法。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=1000
    )
    
    print(response.choices[0].message.content)
    print("\n")

# 示例3: 代码优化
def optimize_code():
    """代码优化示例"""
    print("示例3: 代码优化")
    print("-" * 50)
    
    code_to_optimize = """
    def find_duplicates(arr):
        duplicates = []
        for i in range(len(arr)):
            for j in range(i+1, len(arr)):
                if arr[i] == arr[j] and arr[i] not in duplicates:
                    duplicates.append(arr[i])
        return duplicates
    """
    
    prompt = f"请优化以下代码，提高其性能，同时保持相同的功能：\n{code_to_optimize}"
    
    response = client.chat.completions.create(
        model="deepseek-coder",
        messages=[
            {"role": "system", "content": "你是一个专业的代码优化专家，擅长提高代码效率和可读性。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=1000
    )
    
    print(response.choices[0].message.content)
    print("\n")

# 示例4: 代码补全
def complete_code():
    """代码补全示例"""
    print("示例4: 代码补全")
    print("-" * 50)
    
    incomplete_code = """
    class BinarySearchTree:
        def __init__(self, value=None):
            self.value = value
            self.left = None
            self.right = None
        
        def insert(self, value):
            # 请完成此函数，实现二叉搜索树的插入功能
    """
    
    prompt = f"请完成以下二叉搜索树的插入函数实现：\n{incomplete_code}"
    
    response = client.chat.completions.create(
        model="deepseek-coder",
        messages=[
            {"role": "system", "content": "你是一个专业的数据结构专家，擅长实现各种算法和数据结构。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=1000
    )
    
    print(response.choices[0].message.content)
    print("\n")

# 示例5: 代码调试
def debug_code():
    """代码调试示例"""
    print("示例5: 代码调试")
    print("-" * 50)
    
    buggy_code = """
    def merge_sort(arr):
        if len(arr) <= 1:
            return arr
            
        mid = len(arr) // 2
        left = merge_sort(arr[:mid])
        right = merge_sort(arr[mid:])
        
        return merge(left, right)
    
    def merge(left, right):
        result = []
        i = j = 0
        
        while i < len(left) and j < len(right):
            if left[i] < right[j]:
                result.append(left[i])
                i += 1
            else:
                result.append(right[j])
                j += 1
                
        # 这里有一个错误
        result.extend(right[j:])
        
        return result
    """
    
    prompt = f"以下归并排序代码有一个bug，请找出并修复它：\n{buggy_code}"
    
    response = client.chat.completions.create(
        model="deepseek-coder",
        messages=[
            {"role": "system", "content": "你是一个专业的代码调试专家，擅长发现和修复代码中的错误。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=1000
    )
    
    print(response.choices[0].message.content)
    print("\n")

if __name__ == "__main__":
    print("=== DeepSeek 代码生成 API 使用示例 ===\n")
    
    try:
        generate_code()
        explain_code()
        optimize_code()
        complete_code()
        debug_code()
    except Exception as e:
        print(f"发生错误: {e}") 
