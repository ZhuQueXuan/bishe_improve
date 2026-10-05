import os
import json
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

# 示例1: 基本调用
def basic_completion():
    """基本的API调用示例"""
    print("示例1: 基本调用")
    print("-" * 50)
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "你是一个乐于助人的AI助手。"},
            {"role": "user", "content": "简要介绍一下人工智能的发展历史。"}
        ],
        temperature=0.7,
        max_tokens=1000
    )
    
    print(response.choices[0].message.content)
    print("\n")

# 示例2: 流式响应
def streaming_completion():
    """流式响应示例"""
    print("示例2: 流式响应")
    print("-" * 50)
    
    stream = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "你是一个乐于助人的AI助手。"},
            {"role": "user", "content": "请用三个句子描述春天。"}
        ],
        temperature=0.7,
        max_tokens=300,
        stream=True
    )
    
    # 逐步获取生成内容
    print("流式响应内容: ", end="")
    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print("\n\n")

# 示例3: 多轮对话
def chat_conversation():
    """多轮对话示例"""
    print("示例3: 多轮对话")
    print("-" * 50)
    
    conversation = [
        {"role": "system", "content": "你是一个专业的Python编程助手。"},
        {"role": "user", "content": "如何在Python中读取JSON文件?"},
    ]
    
    # 第一轮对话
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=conversation,
        temperature=0.7,
        max_tokens=500
    )
    
    assistant_response = response.choices[0].message.content
    print("助手: " + assistant_response)
    
    # 将助手回复加入对话历史
    conversation.append({"role": "assistant", "content": assistant_response})
    
    # 用户的后续提问
    user_question = "如何处理可能出现的异常?"
    print("用户: " + user_question)
    conversation.append({"role": "user", "content": user_question})
    
    # 第二轮对话
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=conversation,
        temperature=0.7,
        max_tokens=500
    )
    
    print("助手: " + response.choices[0].message.content)
    print("\n")

# 示例4: 使用高级参数
def advanced_parameters():
    """展示高级参数使用"""
    print("示例4: 使用高级参数")
    print("-" * 50)
    
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "你是一个创意写作AI助手。"},
            {"role": "user", "content": "写一个关于太空探索的短故事，包含想象力和科学元素。"}
        ],
        temperature=0.9,  # 更高的温度值，增加创意性
        max_tokens=800,
        top_p=0.95,      # 控制输出多样性
        frequency_penalty=0.5,  # 减少重复
        presence_penalty=0.5,   # 增加主题多样性
    )
    
    print(response.choices[0].message.content)

if __name__ == "__main__":
    print("=== DeepSeek API 使用示例 ===\n")
    
    try:
        basic_completion()
        streaming_completion()
        chat_conversation()
        advanced_parameters()
    except Exception as e:
        print(f"发生错误: {e}") 
