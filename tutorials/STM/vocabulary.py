# -*- coding: utf-8 -*-
"""
Created on 2024-07-31 (Wed) 09:36:23

# This code is available under the MIT License.
# (c)2010-2011 Nakatani Shuyo / Cybozu Labs Inc.
# (c)2018-2019 Hiroki Iida / Retrieva Inc.

@author: I.Azuma
"""
# %%
import nltk

# %%
wl = nltk.WordNetLemmatizer()

def lemmatize(w0):
    w = wl.lemmatize(w0.lower())
    if w in recover_list: return recover_list[w]
    return w

class Vocabulary:
    def __init__(self, excluds_stopwords=False):
        self.vocas = []         # id to word
        self.vocas_id = dict()  # word to id
        self.docfreq = []       # id to document frequency
        self.excluds_stopwords = excluds_stopwords

    def term_to_id(self, term0):
        term = lemmatize(term0)
        if self.excluds_stopwords and is_stopword(term):
            return None
        if term not in self.vocas_id:
            voca_id = len(self.vocas)
            self.vocas_id[term] = voca_id
            self.vocas.append(term)
            self.docfreq.append(0)
        else:
            voca_id = self.vocas_id[term]
        return voca_id

    def doc_to_ids(self, doc):
        ids_list = []
        words = dict()
        for term in doc:
            id = self.term_to_id(term)
            if id is not None:
                ids_list.append(id)
                if id not in words:
                    words[id] = 1
                    self.docfreq[id] += 1
        if "close" in dir(doc):
            doc.close()
        return ids_list

    def cut_low_freq(self, corpus, threshold=1):
        new_vocas = []
        new_docfreq = []
        self.vocas_id = dict()
        conv_map = dict()
        for id, term in enumerate(self.vocas):
            freq = self.docfreq[id]
            if freq > threshold:
                new_id = len(new_vocas)
                self.vocas_id[term] = new_id
                new_vocas.append(term)
                new_docfreq.append(freq)
                conv_map[id] = new_id
        self.vocas = new_vocas
        self.docfreq = new_docfreq

        def conv(doc):
            new_doc = []
            for id in doc:
                if id in conv_map: new_doc.append(conv_map[id])
            return new_doc
        return [conv(doc) for doc in corpus]

    def __getitem__(self, v):
        return self.vocas[v]

    def size(self):
        return len(self.vocas)

    def is_stopword_id(self, id):
        return self.vocas[id] in stopwords_list