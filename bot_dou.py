"""
    Pequeno script/bot para acessar, ler e gravar portarias e documentos
    publicadas no DOU para órgãos específicos e sessões específicas.

    Necessita instalação de python, pandas, selenium.
    Necessita arquivo chromedriver (Google Chrome) disponível no folder.

    Defina a seção, o período de pesquisa e o nome do órgão para pesquisar.

    Saída: impressão da frase principal da portaria, documento.
    Planilha com o nome da portaria ou documento, a frase principal e menções a SIAPEs ou DAS.

    Routine: to make it run everyday, I use linux crontab (windows task scheduler, I guess), associated with
    an automatic cron MAILTO command.

"""
import os
# re : regular expression -- biblioteca para filtar, localizar, extrair textos ou sub-textos de textos gigantes
import re

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By

options = webdriver.ChromeOptions()
options.add_argument("--headless")


def get_texts(site, process=True):
    """ Realiza a busca no site da imprensa oficial, a partir de uma query organizada
    """
    browser = webdriver.Chrome(options=options)
    browser.get(site)
    res = browser.find_elements(By.CLASS_NAME, "title-marker")
    res2 = list()
    for each in res:
        link = each.find_element(By.TAG_NAME, 'a').get_property('href')
        tab = webdriver.Chrome(options=options)
        tab.get(link)
        out = tab.find_element(By.ID, 'materia').text
        if process:
            get_main_text(out)
        res2.append(out)
        tab.quit()
    browser.quit()
    return res2


def building_query(secao, data, palavra):
    """ Constrói a query a partir das necessidades da pesquisa
    """
    return fr'https://www.in.gov.br/consulta/-/buscar/dou?q={palavra}+&s=do{secao}&exactDate={data}&sortType=0'


def get_main_text(text, db='dou.csv', path='/home/furtado/Documents/Professor/Python2022'):
    """ Extrai os dados do documento e grava em arquivo pandas que é reutilizável
    """
    try:
        data = pd.read_csv(os.path.join(path, f'data/{db}'), sep=';')
        data = data.set_index('document')
    except FileNotFoundError:
        data = pd.DataFrame(columns=['siape', 'das', 'context'])
        data.index.name = 'document'
    das_pattern = r'(DAS \d+.\d+)'
    siape_pattern = r'(SIAPE nº \d+)'
    no_pattern = r'(Nº \d+)'
    lines_no = re.findall(no_pattern, text)
    lines = text.split('\n')
    if not lines_no:
        idx = lines[3]
        lines_no = [' ']
    else:
        idx = lines[3] + lines_no[0]
    if idx not in data.index:
        for i in range(len(lines_no)):
            try:
                data.loc[lines[3] + lines_no[i], 'context'] = lines[5 + i]
                if re.findall(das_pattern, lines[5 + i]):
                    data.loc[lines[3] + lines_no[i], 'das'] = re.findall(das_pattern, lines[5 + i])
                if re.findall(siape_pattern, lines[5 + i]):
                    data.loc[lines[3] + lines_no[i], 'siape'] = re.findall(siape_pattern, lines[5 + i])
                print(lines[5 + i])
            except ValueError:
                break
        data.to_csv(os.path.join(path, f'data/{db}'), sep=';')


if __name__ == '__main__':
    s = 2
    dt = 'semana'
    org = 'ipea'

    query = building_query(s, dt, org)
    txts = get_texts(query)
